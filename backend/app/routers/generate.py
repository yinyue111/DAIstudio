"""Submit generation tasks. Freezes the estimated cost, enqueues the worker,
and refunds immediately if enqueue fails."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import GenAsset, GenTask, UploadedAsset, User
from ..schemas import GenerateIn, TaskOut
from ..services import asset_refs, audit, credits, generation, locks
from ..services.config_store import get_model_config, get_setting
from ..services.content_safety import assert_text_allowed
from ..services.generation import assert_model_snapshot_compatible, model_snapshot
from ..services.rate_limit import incr_window
from ..services.ssrf import (
    SsrfError,
    assert_safe_user_asset_url,
    local_storage_key_from_user_asset_url,
)
from ..services.task_output import build_task_out

router = APIRouter(prefix="/api", tags=["generate"])


_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_VIDEO_RESOLUTIONS = {"480p", "720p", "1080p"}
_VIDEO_RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9"}
_COMMON_PARAM_KEYS = {
    "seed",
    "negative_prompt",
    "negative",
    "reference_width",
    "reference_height",
    "width",
    "height",
}
_IMAGE_PARAM_KEYS = _COMMON_PARAM_KEYS | {"n", "size", "style_reference_image"}
_VIDEO_PARAM_KEYS = _COMMON_PARAM_KEYS | {
    "duration",
    "target_duration",
    "resolution",
    "target_resolution",
    "ratio",
    "reference_image_url",
    "first_frame_image",
    "last_frame_image",
    "style_reference_image",
    "preview_resolution",
    "preview_duration",
}
_SOURCE_META_URL_KEYS = {
    "original_url",
    "original_thumb",
    "source_page_url",
    "selected_url",
    "selected_thumb",
}
_SOURCE_META_TEXT_KEYS = {"source_captured_at", "selected_type"}


def _normalise_reference_dimensions(params: dict) -> None:
    for key in ("reference_width", "reference_height", "width", "height"):
        if params.get(key) is None:
            continue
        try:
            value = int(params[key])
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} 非法")
        if not (1 <= value <= 20000):
            raise HTTPException(400, f"{key} 超出范围")
        params[key] = value


def _validate_params(category: str, params: dict) -> dict:
    """Clamp / reject user-supplied generation params before they reach the
    gateway or any media allocation. Mutates and returns the params dict."""
    params = dict(params or {})
    try:
        raw_size = len(json.dumps(params, ensure_ascii=False))
    except (TypeError, ValueError):
        raise HTTPException(400, "params 必须是可序列化 JSON")
    if raw_size > int(settings.max_generate_params_bytes):
        raise HTTPException(400, "params 过大")
    allowed = _IMAGE_PARAM_KEYS if category == "image" else _VIDEO_PARAM_KEYS
    unknown = sorted(str(k) for k in params if k not in allowed)
    if unknown:
        raise HTTPException(400, f"不支持的生成参数:{','.join(unknown[:5])}")
    _normalise_reference_dimensions(params)
    if params.get("seed") not in (None, ""):
        try:
            seed = int(params["seed"])
        except (TypeError, ValueError):
            raise HTTPException(400, "seed 非法")
        if not (0 <= seed <= 2**32 - 1):
            raise HTTPException(400, "seed 超出范围")
        params["seed"] = seed
    if category == "image":
        if params.get("n") is not None:
            try:
                n = int(params["n"])
            except (TypeError, ValueError):
                raise HTTPException(400, "出图数量 n 非法")
            if not (1 <= n <= settings.max_image_n):
                raise HTTPException(400, f"出图数量需在 1..{settings.max_image_n} 之间")
            params["n"] = n
        size = params.get("size")
        if size is not None:
            m = _SIZE_RE.match(str(size))
            if not m or not (
                0 < int(m.group(1)) <= settings.max_image_dim
                and 0 < int(m.group(2)) <= settings.max_image_dim
            ):
                raise HTTPException(400, f"尺寸非法(最大 {settings.max_image_dim}px)")
    else:  # video
        if params.get("duration") is not None:
            try:
                d = int(params["duration"])
            except (TypeError, ValueError):
                raise HTTPException(400, "时长非法")
            if not (1 <= d <= settings.max_video_seconds):
                raise HTTPException(400, f"时长需在 1..{settings.max_video_seconds} 秒之间")
            params["duration"] = d
        for key in ("resolution", "target_resolution"):
            if params.get(key) is not None and params[key] not in _VIDEO_RESOLUTIONS:
                raise HTTPException(400, f"{key} 仅支持 480p/720p/1080p")
        if params.get("target_duration") is not None:
            try:
                td = int(params["target_duration"])
            except (TypeError, ValueError):
                raise HTTPException(400, "target_duration 非法")
            if not (1 <= td <= settings.max_video_seconds):
                raise HTTPException(400, f"target_duration 需在 1..{settings.max_video_seconds} 秒之间")
            params["target_duration"] = td
        if params.get("ratio") is not None and params["ratio"] not in _VIDEO_RATIOS:
            raise HTTPException(400, "ratio 不支持")
    return params


def _validate_prompt_payload(prompt: dict, instruction: str | None) -> None:
    if instruction is not None and len(instruction) > int(settings.max_prompt_chars):
        raise HTTPException(400, "instruction 过长")
    if prompt:
        try:
            raw = json.dumps(prompt, ensure_ascii=False)
        except (TypeError, ValueError):
            raise HTTPException(400, "prompt 必须是可序列化 JSON")
        if len(raw) > int(settings.max_prompt_chars):
            raise HTTPException(400, "prompt 过长")
        for key in ("final_text", "instruction", "negative", "negative_prompt"):
            value = prompt.get(key)
            if isinstance(value, str) and len(value) > int(settings.max_prompt_chars):
                raise HTTPException(400, f"{key} 过长")


def _clean_source_meta_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > 2048:
        return None
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _clean_source_meta_text(value, *, max_len: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:max_len]


def _source_trace(
    *,
    source_asset_url: str | None,
    source_type: str | None,
    meta: dict | None,
) -> dict:
    """Store bounded provenance metadata for compliance/audit only.

    The trace is written under ``params._source_trace`` and ignored by request
    fingerprinting, so it cannot alter generation idempotency.
    """
    source_meta = meta if isinstance(meta, dict) else {}
    try:
        raw_size = len(json.dumps(source_meta, ensure_ascii=False))
    except (TypeError, ValueError):
        source_meta = {}
        raw_size = 0
    if raw_size > 8192:
        source_meta = {}

    trace: dict = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_asset_url": _clean_source_meta_url(source_asset_url),
        "source_type": source_type if source_type in {"image", "video"} else None,
    }
    for key in _SOURCE_META_URL_KEYS:
        cleaned = _clean_source_meta_url(source_meta.get(key))
        if cleaned:
            trace[key] = cleaned
    for key in _SOURCE_META_TEXT_KEYS:
        cleaned = _clean_source_meta_text(source_meta.get(key))
        if cleaned:
            trace[key] = cleaned
    return {k: v for k, v in trace.items() if v is not None}


def _rate_limit(user_id: int) -> None:
    key = f"gen:rate:{user_id}"
    n = incr_window(key, 3600)
    if n > settings.user_gen_rate_per_hour:
        raise HTTPException(429, "生成过于频繁,请稍后再试")


def _estimate_cost(model, category: str, stage: str, n: int = 1) -> int:
    base = max(0, int(model.cost_credits or 0))
    if category == "video" and stage == "preview":
        # preview should freeze very little; configurable via extra.preview_cost
        try:
            preview_cost = int((model.extra or {}).get("preview_cost", max(1, base // 10)))
        except (TypeError, ValueError):
            preview_cost = max(1, base // 10)
        return max(0, preview_cost)
    if category == "image":
        # image cost scales with the number of images requested
        return base * max(1, n)
    return base


def _estimate_cost_from_snapshot(snapshot: dict, category: str, stage: str, n: int = 1) -> int:
    base = max(0, int(snapshot.get("cost_credits") or 0))
    extra = snapshot.get("extra") or {}
    if category == "video" and stage == "preview":
        try:
            preview_cost = int(extra.get("preview_cost", max(1, base // 10)))
        except (TypeError, ValueError):
            preview_cost = max(1, base // 10)
        return max(0, preview_cost)
    if category == "image":
        return base * max(1, n)
    return base


def _assert_reference_access(db: Session, user_id: int, *urls: str | None) -> None:
    for url in urls:
        if not url:
            continue
        key = local_storage_key_from_user_asset_url(url)
        if not key:
            continue
        row = db.get(UploadedAsset, key)
        if row:
            if row.user_id != user_id:
                raise HTTPException(404, "上传素材不存在")
            continue
        if key.startswith(("upload/", "upload_preview/", "upload_video/", "upload_video_preview/")):
            raise HTTPException(404, "上传素材不存在")
        try:
            asset_refs.generated_asset_reference_path(db, user_id, key)
        except asset_refs.AssetRefError as e:
            raise HTTPException(404, str(e)) from e


def _is_local_user_asset(db: Session, user_id: int, url: str | None) -> bool:
    key = local_storage_key_from_user_asset_url(url)
    if not key:
        return False
    row = db.get(UploadedAsset, key)
    if row and row.user_id == user_id:
        return True
    try:
        asset_refs.generated_asset_reference_path(db, user_id, key)
        return True
    except asset_refs.AssetRefError:
        return False


def _is_local_user_video_asset(db: Session, user_id: int, url: str | None) -> bool:
    key = local_storage_key_from_user_asset_url(url)
    if not key:
        return False
    try:
        asset_refs.generated_video_reference_path(db, user_id, key)
        return True
    except asset_refs.AssetRefError:
        return False


def _video_reference_is_actionable(
    db: Session,
    user_id: int,
    source_asset_url: str | None,
    source_type: str,
    prompt: dict,
    params: dict,
) -> bool:
    if source_type != "video":
        return True
    has_reverse_prompt = bool(prompt and (prompt.get("final_text") or len(prompt.keys()) > 1))
    has_local_first_frame = False
    for key in ("first_frame_image", "reference_image_url", "last_frame_image"):
        value = params.get(key)
        if not value:
            continue
        if _is_local_user_asset(db, user_id, value):
            has_local_first_frame = True
            return True
        try:
            assert_safe_user_asset_url(value)
            return True
        except SsrfError:
            continue
    if has_local_first_frame:
        return True
    # A local generated video can be sampled by the reverse flow, but generation
    # gateways currently need an image first frame; avoid pretending the raw video
    # URL itself will be used as model context.
    if _is_local_user_video_asset(db, user_id, source_asset_url) and has_reverse_prompt:
        return True
    return False


def _default_image_n(db: Session) -> int:
    try:
        n = int(get_setting(db, "image_n", 4))
    except (TypeError, ValueError):
        raise HTTPException(400, "默认出图数量配置非法,请联系管理员")
    if not (1 <= n <= settings.max_image_n):
        raise HTTPException(400, f"默认出图数量需在 1..{settings.max_image_n} 之间,请联系管理员")
    return n


def _normalize_client_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    request_id = value.strip()
    if not request_id:
        return None
    if len(request_id) < 8 or len(request_id) > 128:
        raise HTTPException(400, "client_request_id 长度需在 8..128 之间")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", request_id):
        raise HTTPException(400, "client_request_id 只能包含字母、数字、点、下划线、冒号和短横线")
    return request_id


def _existing_client_request_task(
    db: Session,
    user_id: int,
    client_request_id: str | None,
) -> GenTask | None:
    if not client_request_id:
        return None
    return db.execute(
        select(GenTask)
        .where(
            GenTask.user_id == user_id,
            GenTask.client_request_id == client_request_id,
        )
        .limit(1)
    ).scalar_one_or_none()


def _fingerprint_clean(value):
    if isinstance(value, dict):
        return {
            str(k): _fingerprint_clean(v)
            for k, v in value.items()
            if not str(k).startswith("_")
        }
    if isinstance(value, list):
        return [_fingerprint_clean(v) for v in value]
    return value


def _request_fingerprint(
    *,
    category: str,
    stage: str,
    source_asset_url: str | None,
    source_type: str | None,
    prompt: dict,
    params: dict,
    parent_task_id: int | None,
) -> str:
    payload = {
        "category": category,
        "stage": stage,
        "source_asset_url": source_asset_url,
        "source_type": source_type,
        "prompt": _fingerprint_clean(prompt or {}),
        "params": _fingerprint_clean(params or {}),
        "parent_task_id": parent_task_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _assert_client_request_replay(task: GenTask, request_fingerprint: str) -> None:
    existing = ((task.params or {}).get("_client_request_fingerprint") or "").strip()
    if existing and existing != request_fingerprint:
        raise HTTPException(409, "client_request_id 已用于不同请求,请更换后重试")


def _existing_active_final(db: Session, user_id: int, parent_id: int) -> GenTask | None:
    active = db.execute(
        select(GenTask)
        .where(
            GenTask.user_id == user_id,
            GenTask.category == "video",
            GenTask.stage == "final",
            GenTask.parent_task_id == parent_id,
            GenTask.status.in_(("queued", "running", generation.NEEDS_REVIEW)),
        )
        .order_by(GenTask.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if active is not None:
        return active
    return db.execute(
        select(GenTask)
        .join(GenAsset, GenAsset.task_id == GenTask.id)
        .where(
            GenTask.user_id == user_id,
            GenTask.category == "video",
            GenTask.stage == "final",
            GenTask.parent_task_id == parent_id,
            GenTask.status == "succeeded",
        )
        .order_by(GenTask.id.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.post("/generate", response_model=TaskOut)
def generate(body: GenerateIn, request: Request,
             db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    client_request_id = _normalize_client_request_id(body.client_request_id)
    existing_client_task = _existing_client_request_task(db, user.id, client_request_id)

    params = _validate_params(body.category, body.params)
    _validate_prompt_payload(body.prompt or {}, body.instruction)

    # two-stage video: a `final` render must reference its own preview task
    parent = None
    if body.stage == "final":
        if body.category != "video":
            raise HTTPException(400, "final 阶段仅支持视频高清渲染")
        if not body.parent_task_id:
            raise HTTPException(400, "final 阶段需提供 parent_task_id(预览任务)")
        parent = db.get(GenTask, body.parent_task_id)
        if not parent or parent.user_id != user.id:
            raise HTTPException(404, "预览任务不存在")
        if (
            parent.category != "video"
            or parent.stage != "preview"
            or parent.status != "succeeded"
        ):
            raise HTTPException(400, "final 必须基于已成功的视频预览任务")
        has_preview_asset = db.execute(
            select(GenAsset.id).where(GenAsset.task_id == parent.id).limit(1)
        ).scalar_one_or_none()
        if has_preview_asset is None:
            raise HTTPException(400, "预览任务缺少可用素材,无法高清渲染")

    # A final render is FULLY derived from its own preview — never trust the
    # client's prompt/source/params for final, or a user could pass preview A's
    # id while rendering entirely different content. Otherwise assemble from the
    # request: structured(final_text) OR plain instruction (reverse off).
    if body.stage == "final" and parent:
        prompt = dict(parent.prompt or {})
        source_asset_url = parent.source_asset_url
        source_type = parent.source_type
        task_params = dict(parent.params or {})
    else:
        prompt = {}
        if body.prompt:
            prompt = dict(body.prompt)
        if body.instruction:
            prompt["instruction"] = body.instruction
        if not prompt:
            raise HTTPException(400, "请提供 prompt 或 instruction")
        source_asset_url = body.source_asset_url
        source_type = body.source_type
        task_params = params
    assert_text_allowed(
        db,
        prompt,
        task_params.get("negative_prompt"),
        task_params.get("negative"),
    )

    if body.category == "video" and body.stage == "final" and parent:
        existing_final = _existing_active_final(db, user.id, parent.id)
        if existing_final is not None:
            return build_task_out(db, existing_final)

    model_use = body.category  # image/video
    model = get_model_config(db, model_use)
    if not model or not model.enabled:
        raise HTTPException(400, f"未配置可用的{model_use}模型")

    # SSRF: validate every URL that may be forwarded to us / the gateway. This
    # runs for ALL stages — including `final`, which inherits its preview's
    # params and could carry an unsafe URL written before this guard existed.
    try:
        assert_safe_user_asset_url(source_asset_url)
        for _url_key in ("reference_image_url", "first_frame_image", "last_frame_image", "style_reference_image"):
            assert_safe_user_asset_url(task_params.get(_url_key))
    except SsrfError as e:
        raise HTTPException(400, f"素材链接被安全策略拦截:{e}")
    _assert_reference_access(
        db,
        user.id,
        source_asset_url,
        task_params.get("reference_image_url"),
        task_params.get("first_frame_image"),
        task_params.get("last_frame_image"),
        task_params.get("style_reference_image"),
    )
    if body.category == "video" and body.stage == "preview" and not _video_reference_is_actionable(
        db,
        user.id,
        source_asset_url,
        source_type,
        prompt,
        task_params,
    ):
        raise HTTPException(400, "视频参考缺少可用封面或反推提示词,请先反推视频或选择带封面的素材")

    n_images = int(task_params.get("n") or _default_image_n(db)) \
        if body.category == "image" else 1
    # persist the resolved n so freeze (here), the worker, and settlement all
    # agree — otherwise a defaulted n freezes base*4 but settles base*1 (~75% undercharge).
    if body.category == "image":
        task_params["n"] = n_images
    request_fingerprint = _request_fingerprint(
        category=body.category,
        stage=body.stage,
        source_asset_url=source_asset_url,
        source_type=source_type,
        prompt=prompt,
        params=task_params,
        parent_task_id=body.parent_task_id,
    )
    if existing_client_task is not None:
        _assert_client_request_replay(existing_client_task, request_fingerprint)
        return build_task_out(db, existing_client_task)
    _rate_limit(user.id)
    if body.stage == "final" and parent:
        inherited_snapshot = (dict(parent.params or {}).get("_model_snapshot") or {})
        if inherited_snapshot:
            try:
                assert_model_snapshot_compatible(model, inherited_snapshot)
            except generation.ModelSnapshotMismatchError as e:
                raise HTTPException(409, str(e)) from e
        snapshot = inherited_snapshot or model_snapshot(model)
    else:
        snapshot = model_snapshot(model)
    if body.stage != "final":
        task_params["_source_trace"] = _source_trace(
            source_asset_url=source_asset_url,
            source_type=source_type,
            meta=body.source_asset_meta,
        )
    elif "_source_trace" not in task_params and parent is not None:
        parent_trace = (parent.params or {}).get("_source_trace")
        if isinstance(parent_trace, dict):
            task_params["_source_trace"] = parent_trace
    task_params["_model_snapshot"] = snapshot
    task_params["_client_request_fingerprint"] = request_fingerprint
    cost = _estimate_cost_from_snapshot(snapshot, body.category, body.stage, n_images)

    def replay_conflicting_task() -> TaskOut | None:
        existing = _existing_client_request_task(db, user.id, client_request_id)
        if existing is not None:
            _assert_client_request_replay(existing, request_fingerprint)
            return build_task_out(db, existing)
        if body.category == "video" and body.stage == "final" and parent is not None:
            active = _existing_active_final(db, user.id, parent.id)
            if active is not None:
                return build_task_out(db, active)
        return None

    def create_task() -> TaskOut:
        task = GenTask(
            user_id=user.id,
            source_asset_url=source_asset_url,
            source_type=source_type,
            category=body.category,
            stage=body.stage,
            prompt=prompt,
            model_use=model_use,
            params=task_params,
            client_request_id=client_request_id,
            status="queued",
            cost_frozen=cost,
            parent_task_id=body.parent_task_id,
        )
        db.add(task)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            replay = replay_conflicting_task()
            if replay is not None:
                return replay
            raise

        # freeze estimated credits (raises 400 on insufficient balance)
        try:
            credits.freeze(db, user.id, cost, biz_ref=task.id, commit=False)
        except (credits.InsufficientCredits, ValueError) as e:
            db.rollback()
            raise HTTPException(400, str(e))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            replay = replay_conflicting_task()
            if replay is not None:
                return replay
            raise
        db.refresh(task)

        # enqueue; refund if the broker is unreachable
        try:
            from ..tasks import generate_image_task, generate_video_task

            if body.category == "image":
                generate_image_task.delay(task.id)
            else:
                generate_video_task.delay(task.id)
        except Exception as e:  # noqa: BLE001
            credits.refund(db, user.id, cost, biz_ref=task.id, commit=False)
            task.status = "failed"
            task.error = f"入队失败:{e}"
            db.commit()
            raise HTTPException(503, "任务入队失败,已退回额度。请确认 Worker/Redis 运行中")

        audit.log(db, user_id=user.id, action="generate", biz_type="gen_task",
                  biz_id=task.id, ip=get_client_ip(request),
                  detail={"category": body.category, "stage": body.stage, "cost": cost})
        return TaskOut.model_validate(task)

    if body.category == "video" and body.stage == "final" and parent:
        lock_key = f"gen:final:create:{user.id}:{parent.id}"
        lock_token = locks.acquire(lock_key, ttl=60)
        if not lock_token:
            active = _existing_active_final(db, user.id, parent.id)
            if active:
                return build_task_out(db, active)
            raise HTTPException(409, "高清视频任务正在创建,请稍后刷新")
        try:
            active = _existing_active_final(db, user.id, parent.id)
            if active:
                return build_task_out(db, active)
            return create_task()
        finally:
            locks.release(lock_key, lock_token)

    return create_task()
