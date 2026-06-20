"""Link parsing. Runs inline (fast) with a short-lived cache so the demo works
even when no Celery worker is running; SSRF-guarded inside the fetcher."""
from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import ParseRecord, UploadedAsset, User
from ..redis_client import redis_client
from ..schemas import ParseIn, ParseOut
from ..services import audit, gateway, storage
from ..services.fetcher import parse_url
from ..services.safe_logging import redact_url_for_log
from ..services.ssrf import SsrfError
from ..services.watermark import make_image_preview, make_model_reference

router = APIRouter(prefix="/api", tags=["parse"])
log = logging.getLogger("parse")
_LOCALIZE_IMAGE_LIMIT = 8


def _cache_key(url: str, user_id: int) -> str:
    # scope per user so a cache hit never returns another user's ParseRecord id
    return f"parse:{user_id}:" + hashlib.sha256(url.encode()).hexdigest()


def _rate_limit(user_id: int) -> None:
    key = f"parse:rate:{user_id}"
    n = redis_client.incr(key)
    if n == 1:
        redis_client.expire(key, 3600)
    if n > settings.user_parse_rate_per_hour:
        raise HTTPException(429, "抓取过于频繁,请稍后再试")


def _localize_media_url(url: str | None, db: Session | None = None, user_id: int | None = None) -> str | None:
    if not url:
        return None
    if storage.key_from_url(url):
        return url
    try:
        raw = gateway.download_bytes_limited(
            url,
            max_bytes=int(settings.parse_localize_image_max_bytes),
            allowed_content_types=("image/",),
        )
        try:
            img = Image.open(io.BytesIO(raw))
            width, height = img.size
        except (UnidentifiedImageError, OSError, ValueError) as e:
            raise ValueError("不是有效图片") from e
        if width <= 0 or height <= 0:
            raise ValueError("图片尺寸非法")
        if width * height > int(settings.parse_localize_image_max_pixels):
            raise ValueError("图片像素过大")
        preview_png, preview_w, preview_h = make_image_preview(
            raw,
            max_pixels=int(settings.parse_localize_image_max_pixels),
        )
        model_ref_png, _, _ = make_model_reference(
            raw,
            max_pixels=int(settings.parse_localize_image_max_pixels),
        )
        key = storage.save_bytes(preview_png, "preview", "png")
        model_ref_key = storage.save_bytes_named(
            model_ref_png,
            "model_ref",
            key.split("/", 1)[1],
        )
        if db is not None and user_id is not None:
            db.merge(
                UploadedAsset(
                    key=key,
                    user_id=user_id,
                    mime="image/png",
                    width=preview_w,
                    height=preview_h,
                    bytes=len(preview_png),
                    original_filename="parsed-preview.png",
                )
            )
            db.merge(
                UploadedAsset(
                    key=model_ref_key,
                    user_id=user_id,
                    mime="image/png",
                    width=preview_w,
                    height=preview_h,
                    bytes=len(model_ref_png),
                    original_filename="parsed-model-ref.png",
                )
            )
        return storage.public_url(key)
    except Exception as e:  # noqa: BLE001
        log.info("parse media localize skipped url=%s error=%s", redact_url_for_log(url), e)
        return None


def _localize_assets(assets: list[dict], db: Session | None = None,
                     user_id: int | None = None) -> list[dict]:
    out: list[dict] = []
    localized_count = 0
    for asset in assets:
        item = dict(asset)
        if localized_count < _LOCALIZE_IMAGE_LIMIT and item.get("type") == "image":
            local = _localize_media_url(item.get("url"), db=db, user_id=user_id)
            if local:
                item["original_url"] = item.get("url")
                if item.get("thumb"):
                    item["original_thumb"] = item.get("thumb")
                item["url"] = local
                item["thumb"] = local
                localized_count += 1
        elif localized_count < _LOCALIZE_IMAGE_LIMIT and item.get("type") == "video":
            local_thumb = _localize_media_url(item.get("thumb"), db=db, user_id=user_id)
            if local_thumb:
                item["original_thumb"] = item.get("thumb")
                item["thumb"] = local_thumb
                localized_count += 1
            else:
                item["original_thumb"] = item.get("thumb")
                item["thumb"] = None
        out.append(item)
    return out


@router.post("/parse", response_model=ParseOut)
def submit_parse(body: ParseIn, request: Request,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "链接不能为空")

    # short-term cache: reuse THIS user's most recent successful parse of this URL
    cached_id = redis_client.get(_cache_key(url, user.id))
    if cached_id:
        rec = db.get(ParseRecord, int(cached_id))
        if rec and rec.status == "done" and rec.user_id == user.id:
            return ParseOut(id=rec.id, status=rec.status, url=rec.url, assets=rec.assets)

    # only count real fetches against the rate limit (cache hits are free)
    _rate_limit(user.id)

    rec = ParseRecord(user_id=user.id, url=url, status="queued")
    db.add(rec)
    db.commit()
    db.refresh(rec)

    try:
        assets = _localize_assets(parse_url(url), db=db, user_id=user.id)
        rec.assets = assets
        rec.status = "done"
        rec.cached_until = datetime.now(timezone.utc) + timedelta(
            minutes=settings.parse_cache_minutes
        )
        db.commit()
        redis_client.setex(_cache_key(url, user.id), settings.parse_cache_minutes * 60, str(rec.id))
        audit.log(db, user_id=user.id, action="parse", biz_type="parse",
                  biz_id=rec.id, ip=get_client_ip(request),
                  detail={"url": redact_url_for_log(url), "n": len(assets)})
    except SsrfError as e:
        rec.status = "failed"
        rec.error = str(e)
        db.commit()
        raise HTTPException(400, f"链接被安全策略拦截:{e}")
    except ValueError as e:
        # fetcher normalises network/empty-result failures to clean ValueErrors
        rec.status = "failed"
        rec.error = str(e)
        db.commit()
        raise HTTPException(422, f"抓取失败:{e}")
    except Exception as e:  # noqa: BLE001
        # unexpected error: keep details server-side, give the user a clean hint
        log.exception("parse failed for url=%s", redact_url_for_log(url))
        rec.status = "failed"
        rec.error = str(e)[:500]
        db.commit()
        raise HTTPException(422, "抓取失败,请稍后重试或更换链接")

    return ParseOut(id=rec.id, status=rec.status, url=rec.url, assets=rec.assets)


@router.get("/parse/{parse_id}", response_model=ParseOut)
def get_parse(parse_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    rec = db.get(ParseRecord, parse_id)
    if not rec or rec.user_id != user.id:
        raise HTTPException(404, "解析记录不存在")
    return ParseOut(id=rec.id, status=rec.status, url=rec.url,
                    assets=rec.assets, error=rec.error)
