"""Link parsing. Submit quickly, then fetch/localize in a Celery worker."""
from __future__ import annotations

import hashlib
import io
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import ParseRecord, UploadedAsset, User
from ..redis_client import redis_client
from ..schemas import ParseIn, ParseOut
from ..services import audit, gateway, project_collection, storage, user_assets
from ..services.fetcher import extract_first_url, parse_url
from ..services.rate_limit import incr_window
from ..services.safe_logging import redact_url_for_log
from ..services.ssrf import SsrfError
from ..services.upload_quota import ensure_user_media_quota
from ..services.watermark import make_image_preview, make_model_reference

router = APIRouter(prefix="/api", tags=["parse"])
log = logging.getLogger("parse")
_REVERSE_MODEL_REF_MAX_SIDE = 1024
_REVERSE_MODEL_REF_QUALITY = 92


class LocalizedMedia(dict):
    url: str
    width: int | None
    height: int | None
    asset_ref: str | None


def _localized_url(local: LocalizedMedia | str) -> str:
    if isinstance(local, str):
        return local
    return str(local["url"])


def _localized_int(local: LocalizedMedia | str, key: str) -> int | None:
    if isinstance(local, str):
        return None
    value = local.get(key)
    return int(value) if value else None


def _localized_asset_ref(local: LocalizedMedia | str) -> str | None:
    if isinstance(local, str):
        return None
    value = str(local.get("asset_ref") or "").strip()
    return value or None


def _cache_key(url: str, user_id: int) -> str:
    # scope per user so a cache hit never returns another user's ParseRecord id
    return f"parse:{user_id}:" + hashlib.sha256(url.encode()).hexdigest()


def _rate_limit(user_id: int) -> None:
    key = f"parse:rate:{user_id}"
    n = incr_window(key, 3600)
    if n > settings.user_parse_rate_per_hour:
        raise HTTPException(429, "抓取过于频繁,请稍后再试")


def _pending_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=max(1, int(settings.parse_pending_max_age_minutes)))


def _existing_active_parse(db: Session, user_id: int, url: str) -> ParseRecord | None:
    return db.execute(
        select(ParseRecord)
        .where(
            ParseRecord.user_id == user_id,
            ParseRecord.url == url,
            ParseRecord.status.in_(("queued", "running")),
            ParseRecord.created_at >= _pending_cutoff(),
        )
        .order_by(ParseRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _assert_parse_capacity(db: Session, user_id: int) -> None:
    pending = db.execute(
        select(func.count())
        .select_from(ParseRecord)
        .where(
            ParseRecord.user_id == user_id,
            ParseRecord.status.in_(("queued", "running")),
            ParseRecord.created_at >= _pending_cutoff(),
        )
    ).scalar_one()
    if int(pending or 0) >= int(settings.parse_pending_limit):
        raise HTTPException(429, "抓取任务过多,请稍后再试")


def _localize_media_url(
    url: str | None,
    db: Session | None = None,
    user_id: int | None = None,
    *,
    timeout_seconds: int | None = None,
    register_asset: bool = True,
    raw_bytes: bytes | None = None,
) -> LocalizedMedia | None:
    if not url:
        return None
    if storage.key_from_url(url):
        return LocalizedMedia(url=url, width=None, height=None)
    try:
        raw = raw_bytes
        if raw is None:
            raw = _download_media_bytes(
                url,
                timeout_seconds=int(
                    timeout_seconds or settings.parse_localize_download_timeout_seconds
                ),
            )
        if raw is None:
            return None
        try:
            img = Image.open(io.BytesIO(raw))
            width, height = img.size
        except (UnidentifiedImageError, OSError, ValueError) as e:
            raise ValueError("不是有效图片") from e
        if width <= 0 or height <= 0:
            raise ValueError("图片尺寸非法")
        if width * height > int(settings.parse_localize_image_max_pixels):
            raise ValueError("图片像素过大")
        normalized_png = b""
        if register_asset and db is not None and user_id is not None:
            img.load()
            if (img.format or "").lower() == "gif" and getattr(img, "is_animated", False):
                img.seek(0)
            has_alpha = "A" in img.getbands() or "transparency" in img.info
            normalized = img.convert("RGBA" if has_alpha else "RGB")
            if has_alpha and normalized.getchannel("A").getextrema()[0] >= 255:
                normalized = normalized.convert("RGB")
            normalized_buf = io.BytesIO()
            normalized.save(normalized_buf, format="PNG")
            normalized_png = normalized_buf.getvalue()
        preview_png, preview_w, preview_h = make_image_preview(
            raw,
            max_pixels=int(settings.parse_localize_image_max_pixels),
        )
        model_ref_jpeg, _, _ = make_model_reference(
            raw,
            max_side=_REVERSE_MODEL_REF_MAX_SIDE,
            max_pixels=int(settings.parse_localize_image_max_pixels),
            quality=_REVERSE_MODEL_REF_QUALITY,
            subsampling=0,
        )
        if db is not None and user_id is not None:
            ensure_user_media_quota(
                db,
                user_id,
                len(preview_png) + len(model_ref_jpeg) + len(normalized_png),
            )
        stem = uuid.uuid4().hex
        saved_keys: list[str] = []
        upload_key = None
        try:
            if normalized_png:
                upload_key = storage.save_bytes_named(
                    normalized_png,
                    "upload",
                    f"{stem}.png",
                )
                saved_keys.append(upload_key)
            key = storage.save_bytes_named(preview_png, "preview", f"{stem}.png")
            saved_keys.append(key)
            model_ref_key = storage.save_bytes_named(
                model_ref_jpeg,
                "model_ref",
                f"{stem}.jpg",
            )
            saved_keys.append(model_ref_key)
        except Exception:
            for saved_key in saved_keys:
                try:
                    storage.delete(saved_key)
                except Exception:  # noqa: BLE001
                    pass
            raise
        if db is not None and user_id is not None:
            if upload_key is not None:
                db.merge(
                    UploadedAsset(
                        key=upload_key,
                        user_id=user_id,
                        mime="image/png",
                        width=width,
                        height=height,
                        bytes=len(normalized_png),
                        original_filename="parsed-image.png",
                        origin="fetched",
                    )
                )
            db.merge(
                UploadedAsset(
                    key=key,
                    user_id=user_id,
                    mime="image/png",
                    width=preview_w,
                    height=preview_h,
                    bytes=len(preview_png),
                    original_filename="parsed-preview.png",
                    origin="fetched",
                )
            )
            db.merge(
                UploadedAsset(
                    key=model_ref_key,
                    user_id=user_id,
                    mime="image/jpeg",
                    width=preview_w,
                    height=preview_h,
                    bytes=len(model_ref_jpeg),
                    original_filename="parsed-model-ref.jpg",
                    origin="fetched",
                )
            )
        return LocalizedMedia(
            url=storage.public_url(key),
            width=preview_w,
            height=preview_h,
            asset_ref=(
                user_assets.uploaded_asset_ref(upload_key)
                if upload_key is not None
                else None
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.info("parse media localize skipped url=%s error=%s", redact_url_for_log(url), e)
        return None


def _download_media_bytes(url: str | None, *, timeout_seconds: int) -> bytes | None:
    if not url:
        return None
    try:
        return gateway.download_bytes_limited(
            url,
            max_bytes=int(settings.parse_localize_image_max_bytes),
            allowed_content_types=("image/",),
            timeout_seconds=timeout_seconds,
        )
    except Exception as e:  # noqa: BLE001
        log.info("parse media download skipped url=%s error=%s", redact_url_for_log(url), e)
        return None


def _localize_assets(
    assets: list[dict],
    db: Session | None = None,
    user_id: int | None = None,
    *,
    source_page_url: str | None = None,
    captured_at: datetime | None = None,
) -> list[dict]:
    out: list[dict] = []
    captured_iso = (captured_at or datetime.now(timezone.utc)).isoformat()
    localized_count = 0
    max_localized = max(1, int(settings.parse_localize_max_assets or 1))
    parallelism = max(1, int(settings.parse_localize_parallelism or 1))
    total_timeout = max(1, int(settings.parse_localize_total_timeout_seconds or 1))
    deadline = time.monotonic() + total_timeout
    next_index = 0
    while next_index < len(assets):
        if localized_count >= max_localized:
            log.info(
                "parse asset localization cap reached localized=%s cap=%s total_assets=%s",
                localized_count,
                max_localized,
                len(assets),
            )
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.info(
                "parse asset localization budget exhausted localized=%s budget_seconds=%s total_assets=%s",
                localized_count,
                total_timeout,
                len(assets),
            )
            break
        batch_size = min(
            parallelism,
            max_localized - localized_count,
            len(assets) - next_index,
        )
        batch = [dict(asset) for asset in assets[next_index:next_index + batch_size]]
        next_index += batch_size
        remaining_timeout = max(
            1,
            min(
                int(settings.parse_localize_download_timeout_seconds),
                int(remaining),
            ),
        )
        source_urls = [
            item.get("url") if item.get("type") == "image" else item.get("thumb")
            for item in batch
        ]
        with ThreadPoolExecutor(max_workers=batch_size, thread_name_prefix="parse-media") as pool:
            download = partial(
                _download_media_bytes,
                timeout_seconds=remaining_timeout,
            )
            downloaded = list(pool.map(download, source_urls))

        for item, raw in zip(batch, downloaded, strict=True):
            item["source_page_url"] = source_page_url
            item["source_captured_at"] = captured_iso
            if item.get("type") == "image":
                local = _localize_media_url(
                    item.get("url"),
                    db=db,
                    user_id=user_id,
                    timeout_seconds=remaining_timeout,
                    raw_bytes=raw,
                ) if raw is not None else None
                if local:
                    localized_count += 1
                    item["original_url"] = item.get("url")
                    if item.get("thumb"):
                        item["original_thumb"] = item.get("thumb")
                    item["url"] = _localized_url(local)
                    item["thumb"] = _localized_url(local)
                    asset_ref = _localized_asset_ref(local)
                    if asset_ref:
                        item["asset_ref"] = asset_ref
                    if not item.get("width") and _localized_int(local, "width"):
                        item["width"] = _localized_int(local, "width")
                    if not item.get("height") and _localized_int(local, "height"):
                        item["height"] = _localized_int(local, "height")
                else:
                    item["original_url"] = item.get("url")
                    if item.get("thumb"):
                        item["original_thumb"] = item.get("thumb")
                    # Do not hand external image URLs to the browser: production CSP
                    # intentionally only allows platform-owned media origins. If an
                    # image cannot be localized, omit it from the selectable assets.
                    continue
            elif item.get("type") == "video":
                local_thumb = _localize_media_url(
                    item.get("thumb"),
                    db=db,
                    user_id=user_id,
                    timeout_seconds=remaining_timeout,
                    register_asset=False,
                    raw_bytes=raw,
                ) if raw is not None else None
                if local_thumb:
                    localized_count += 1
                    item["original_thumb"] = item.get("thumb")
                    item["thumb"] = _localized_url(local_thumb)
                    if not item.get("thumb_width") and _localized_int(local_thumb, "width"):
                        item["thumb_width"] = _localized_int(local_thumb, "width")
                    if not item.get("thumb_height") and _localized_int(local_thumb, "height"):
                        item["thumb_height"] = _localized_int(local_thumb, "height")
                else:
                    item["original_thumb"] = item.get("thumb")
                    item["thumb"] = None
            out.append(item)
    return out


def run_parse_record(parse_id: int) -> None:
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        claimed = db.execute(
            update(ParseRecord)
            .where(ParseRecord.id == parse_id, ParseRecord.status == "queued")
            .values(status="running")
        ).rowcount
        if (claimed or 0) != 1:
            db.rollback()
            return
        db.commit()
        rec = db.get(ParseRecord, parse_id)
        if not rec:
            return
        try:
            started = time.monotonic()
            raw_assets = parse_url(rec.url)
            assets = _localize_assets(
                raw_assets,
                db=db,
                user_id=rec.user_id,
                source_page_url=rec.url,
                captured_at=datetime.now(timezone.utc),
            )
            rec.assets = assets
            rec.status = "done"
            rec.cached_until = datetime.now(timezone.utc) + timedelta(
                minutes=settings.parse_cache_minutes
            )
            project_collection.collect_parse_outputs(db, int(rec.id), assets)
            db.commit()
            redis_client.setex(_cache_key(rec.url, rec.user_id), settings.parse_cache_minutes * 60, str(rec.id))
            log.info(
                "parse completed id=%s raw_assets=%s localized_assets=%s elapsed=%.2fs url=%s",
                rec.id,
                len(raw_assets),
                len(assets),
                time.monotonic() - started,
                redact_url_for_log(rec.url),
            )
        except SsrfError as e:
            rec.status = "failed"
            rec.error = f"链接被安全策略拦截:{e}"
            db.commit()
        except ValueError as e:
            rec.status = "failed"
            rec.error = f"抓取失败:{e}"
            db.commit()
        except Exception:  # noqa: BLE001
            log.exception("parse failed for url=%s", redact_url_for_log(rec.url))
            rec.status = "failed"
            rec.error = "抓取失败,请稍后重试或更换链接"
            db.commit()
    finally:
        db.close()


@router.post("/parse", response_model=ParseOut)
def submit_parse(body: ParseIn, request: Request,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    raw_url = body.url.strip()
    url = extract_first_url(raw_url) or raw_url
    if not url:
        raise HTTPException(400, "链接不能为空")

    project = None
    if body.project_id is not None:
        try:
            project = project_collection.require_owned_project(db, user.id, body.project_id)
        except project_collection.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    # short-term cache: reuse THIS user's most recent successful parse of this URL
    cached_id = redis_client.get(_cache_key(url, user.id))
    if cached_id:
        rec = db.get(ParseRecord, int(cached_id))
        if rec and rec.status == "done" and rec.user_id == user.id:
            if project is not None:
                project_collection.attach_task(
                    db,
                    project=project,
                    task_kind="parse",
                    task_id=int(rec.id),
                )
                db.flush()
                project_collection.collect_parse_outputs(db, int(rec.id), rec.assets)
                db.commit()
            return ParseOut(id=rec.id, status=rec.status, url=rec.url, assets=rec.assets)

    active = _existing_active_parse(db, user.id, url)
    if active:
        if project is not None:
            project_collection.attach_task(
                db,
                project=project,
                task_kind="parse",
                task_id=int(active.id),
            )
            db.commit()
        return ParseOut(id=active.id, status=active.status, url=active.url, assets=active.assets, error=active.error)

    # only count real fetches against the rate limit (cache hits are free)
    _rate_limit(user.id)
    _assert_parse_capacity(db, user.id)

    rec = ParseRecord(user_id=user.id, url=url, status="queued")
    db.add(rec)
    db.flush()
    if project is not None:
        project_collection.attach_task(
            db,
            project=project,
            task_kind="parse",
            task_id=int(rec.id),
        )
    db.commit()
    db.refresh(rec)

    try:
        from ..tasks import enqueue_with_request_context, parse_url_task

        enqueue_with_request_context(parse_url_task, rec.id)
        db.refresh(rec)
    except Exception as e:  # noqa: BLE001
        log.exception("failed to enqueue parse for url=%s", redact_url_for_log(url))
        rec.status = "failed"
        rec.error = f"抓取任务入队失败:{e}"
        db.commit()
        raise HTTPException(503, "抓取任务入队失败,请确认 Worker/Redis 运行中")

    if rec.status == "done":
        asset_count = len(rec.assets or [])
    else:
        asset_count = 0
    audit.log(db, user_id=user.id, action="parse", biz_type="parse",
              biz_id=rec.id, ip=get_client_ip(request),
              detail={"url": redact_url_for_log(url), "status": rec.status, "n": asset_count})
    return ParseOut(id=rec.id, status=rec.status, url=rec.url, assets=rec.assets)


@router.get("/parse/{parse_id}", response_model=ParseOut)
def get_parse(parse_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    rec = db.get(ParseRecord, parse_id)
    if not rec or rec.user_id != user.id:
        raise HTTPException(404, "解析记录不存在")
    return ParseOut(id=rec.id, status=rec.status, url=rec.url,
                    assets=rec.assets, error=rec.error)
