"""Submit generation tasks. Freezes the estimated cost, enqueues the worker,
and refunds immediately if enqueue fails."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import GenAsset, GenTask, UploadedAsset, User
from ..redis_client import redis_client
from ..schemas import GenerateIn, TaskOut
from ..services import asset_refs, audit, credits, locks
from ..services.config_store import get_model_config, get_setting
from ..services.ssrf import (
    SsrfError,
    assert_safe_user_asset_url,
    local_storage_key_from_user_asset_url,
)

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
_IMAGE_PARAM_KEYS = _COMMON_PARAM_KEYS | {"n", "size"}
_VIDEO_PARAM_KEYS = _COMMON_PARAM_KEYS | {
    "duration",
    "target_duration",
    "resolution",
    "target_resolution",
    "ratio",
    "reference_image_url",
    "first_frame_image",
    "preview_resolution",
    "preview_duration",
}


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


def _rate_limit(user_id: int) -> None:
    key = f"gen:rate:{user_id}"
    n = redis_client.incr(key)
    if n == 1:
        redis_client.expire(key, 3600)
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
        if key.startswith(("upload/", "upload_preview/")):
            raise HTTPException(404, "上传素材不存在")
        try:
            asset_refs.generated_asset_reference_path(db, user_id, key)
        except asset_refs.AssetRefError as e:
            raise HTTPException(404, str(e)) from e


def _default_image_n(db: Session) -> int:
    try:
        n = int(get_setting(db, "image_n", 4))
    except (TypeError, ValueError):
        raise HTTPException(400, "默认出图数量配置非法,请联系管理员")
    if not (1 <= n <= settings.max_image_n):
        raise HTTPException(400, f"默认出图数量需在 1..{settings.max_image_n} 之间,请联系管理员")
    return n


def _existing_active_final(db: Session, user_id: int, parent_id: int) -> GenTask | None:
    return db.execute(
        select(GenTask)
        .where(
            GenTask.user_id == user_id,
            GenTask.category == "video",
            GenTask.stage == "final",
            GenTask.parent_task_id == parent_id,
            GenTask.status.in_(("queued", "running", "succeeded")),
        )
        .order_by(GenTask.id.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.post("/generate", response_model=TaskOut)
def generate(body: GenerateIn, request: Request,
             db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    params = _validate_params(body.category, body.params)
    _validate_prompt_payload(body.prompt or {}, body.instruction)
    _rate_limit(user.id)

    model_use = body.category  # image/video
    model = get_model_config(db, model_use)
    if not model or not model.enabled:
        raise HTTPException(400, f"未配置可用的{model_use}模型")

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
        existing_final = _existing_active_final(db, user.id, parent.id)
        if existing_final is not None:
            return TaskOut.model_validate(existing_final)

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

    # SSRF: validate every URL that may be forwarded to us / the gateway. This
    # runs for ALL stages — including `final`, which inherits its preview's
    # params and could carry an unsafe URL written before this guard existed.
    try:
        assert_safe_user_asset_url(source_asset_url)
        for _url_key in ("reference_image_url", "first_frame_image"):
            assert_safe_user_asset_url(task_params.get(_url_key))
    except SsrfError as e:
        raise HTTPException(400, f"素材链接被安全策略拦截:{e}")
    _assert_reference_access(
        db,
        user.id,
        source_asset_url,
        task_params.get("reference_image_url"),
        task_params.get("first_frame_image"),
    )

    n_images = int(task_params.get("n") or _default_image_n(db)) \
        if body.category == "image" else 1
    # persist the resolved n so freeze (here), the worker, and settlement all
    # agree — otherwise a defaulted n freezes base*4 but settles base*1 (~75% undercharge).
    if body.category == "image":
        task_params["n"] = n_images
    cost = _estimate_cost(model, body.category, body.stage, n_images)

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
            status="queued",
            cost_frozen=cost,
            parent_task_id=body.parent_task_id,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # freeze estimated credits (raises 400 on insufficient balance)
        try:
            credits.freeze(db, user.id, cost, biz_ref=task.id)
        except (credits.InsufficientCredits, ValueError) as e:
            task.status = "failed"
            task.error = str(e)
            db.commit()
            raise HTTPException(400, str(e))

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
        if not locks.acquire(lock_key, ttl=60):
            active = _existing_active_final(db, user.id, parent.id)
            if active:
                return TaskOut.model_validate(active)
            raise HTTPException(409, "高清视频任务正在创建,请稍后刷新")
        try:
            active = _existing_active_final(db, user.id, parent.id)
            if active:
                return TaskOut.model_validate(active)
            return create_task()
        finally:
            locks.release(lock_key)

    return create_task()
