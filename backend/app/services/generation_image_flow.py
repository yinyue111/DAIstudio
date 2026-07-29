"""Image generation task flow."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from ..config import settings
from ..db import SessionLocal
from ..models import GenAsset, GenTask
from . import credits, gateway, locks, storage, usage
from .config_store import get_model_config, get_setting
from .content_safety import assert_generated_media_allowed
from .generation_common import (
    TaskCanceled,
    TaskLockedError,
    cancel_and_refund,
    fail_and_refund,
    mark_needs_review,
    publish_task_update,
    raise_if_cancel_requested,
    settlement_cost,
)
from .generation_image_evidence import (
    ReviewedEvidenceMaskError,
    attach_mask_metadata_to_generation_revision,
    rasterize_reviewed_evidence_mask,
    resolve_reviewed_evidence_plan,
)
from .generation_media import (
    EDIT_MASK_SEND_CONFIDENCE,
    SubjectProtectionBusy,
    closest_image_size,
    composite_product_subject_pixels,
    final_prompt,
    gateway_image_edit_mask,
    gateway_reference_image,
    reference_dimensions,
)
from .generation_model_runtime import (
    gen_image_with_model_config,
    model_config_for_task,
    model_from_snapshot,
)
from .generation_prompts import (
    generation_prompt_for_model,
    is_portrait_generation_task,
    is_product_generation_task,
    portrait_image_negative_prompt,
    portrait_negative_prompt_evidence,
    product_fidelity_prompt,
    product_image_negative_prompt,
)
from .generation_state import NEEDS_REVIEW, claim_terminal
from .generation_state import TERMINAL_STATUSES as TERMINAL_STATUSES
from .generation_video_flow import unlink_keys
from .media_sidecars import storage_bytes_for_asset_urls, storage_bytes_for_keys
from .progress import set_progress
from .watermark import dimensions, image_ext, make_image_preview, make_model_reference

log = logging.getLogger("generation")

IMAGE_EDIT_REFERENCE_MAX_SIDE = 1024
IMAGE_PRODUCT_EDIT_REFERENCE_MAX_SIDE = 1536
IMAGE_PORTRAIT_EDIT_REFERENCE_MAX_SIDE = 1536
IMAGE_STYLE_REFERENCE_MAX_SIDE = 768
IMAGE_EDIT_MASK_RETRY_DELAY_SECONDS = 0.12


class ProductProtectionUnavailable(RuntimeError):
    """Product edit protection could not be applied reliably."""


@dataclass
class _ImageRequest:
    task: GenTask
    model: Any
    n: int
    params: dict
    is_product: bool
    is_portrait: bool
    size: str
    prompt: str


@dataclass
class _ImageRenderPlan:
    ref: str | None
    ref_content_hash: str | None
    edit_refs: list[str] | None
    reference_max_side: int
    edit_source_url: str | None
    edit_path: str | None
    extra_payload: dict
    mask_result: Any = None
    product_pixel_lock: bool = False
    product_pixel_lock_label: str = "strict"
    mode: str = "txt2img"


@dataclass
class _ImageGatewayOutcome:
    images: list[bytes]
    failure_items: list[Any]
    failures: list[str]
    has_unknown_failure: bool
    echo_sizes: list[str]
    echo_qualities: list[str]
    echo_formats: list[str]
    echo_models: list[str]
    selected_sources: list[str]
    latency_ms: int


@dataclass
class _ImagePersistenceOutcome:
    written_keys: list[str]
    saved_count: int
    image_errors: list[str]
    actual_sizes: list[str]
    returned_sizes: list[str]
    pending_assets: list[GenAsset]
    pixel_lock_params_update: dict


def acquire_image_terminal_boundary(db, task_id: int) -> GenTask | None:
    """Serialize image success with running-task cancellation."""
    if db.get_bind().dialect.name == "sqlite":
        claimed = db.execute(
            update(GenTask)
            .where(GenTask.id == task_id, GenTask.status == "running")
            .values(status=GenTask.status)
        ).rowcount
        if (claimed or 0) != 1:
            return None
        task = db.get(GenTask, task_id, populate_existing=True)
    else:
        task = db.execute(
            select(GenTask)
            .where(GenTask.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if not task or task.status != "running":
            return None
    if task:
        db.refresh(task)
    return task


def image_review_has_local_results(task: GenTask) -> bool:
    """Whether an image review task can settle from already saved result keys."""
    if task.category != "image":
        return False
    params = task.params or {}
    keys = list(params.get("_image_result_keys") or [])
    hd_keys = [key for key in keys if isinstance(key, str) and key.startswith("hd/")]
    preview_keys = [key for key in keys if isinstance(key, str) and key.startswith("preview/")]
    if not hd_keys or not preview_keys:
        return False
    try:
        return all(storage.exists(key) for key in [*hd_keys, *preview_keys])
    except ValueError:
        return False


def image_result_key_pairs(keys: list[str]) -> list[tuple[str, str]]:
    hd_keys = [key for key in keys if isinstance(key, str) and key.startswith("hd/")]
    preview_keys = [key for key in keys if isinstance(key, str) and key.startswith("preview/")]
    pairs: list[tuple[str, str]] = []
    for idx, hd_key in enumerate(hd_keys):
        if not preview_keys:
            break
        pairs.append((hd_key, preview_keys[min(idx, len(preview_keys) - 1)]))
    return pairs


def persist_local_image_result_assets(db, task: GenTask, keys: list[str] | None) -> int:
    """Expose already-saved image results before a held task is reconciled."""
    if not keys:
        return 0
    existing = db.execute(
        select(GenAsset.hd_url).where(GenAsset.task_id == task.id)
    ).scalars().all()
    existing_hd_urls = {str(url) for url in existing if url}
    added = 0
    for hd_key, preview_key in image_result_key_pairs(keys):
        hd_url = storage.public_url(hd_key)
        if not hd_url or hd_url in existing_hd_urls:
            continue
        try:
            hd_path = storage.local_path(hd_key)
            preview_path = storage.local_path(preview_key)
        except Exception as e:  # noqa: BLE001
            log.warning("image task %s local result key skipped: %s", task.id, e)
            continue
        if not hd_path.exists() or not preview_path.exists():
            continue
        try:
            hd_w, hd_h = dimensions(hd_path.read_bytes())
        except Exception:  # noqa: BLE001
            hd_w, hd_h = None, None
        db.add(
            GenAsset(
                task_id=task.id,
                user_id=task.user_id,
                type="image",
                preview_url=storage.public_url(preview_key),
                hd_url=hd_url,
                watermarked=False,
                unlocked=True,
                width=hd_w,
                height=hd_h,
                bytes=storage_bytes_for_asset_urls(
                    storage.public_url(preview_key),
                    hd_url,
                ),
            )
        )
        existing_hd_urls.add(hd_url)
        added += 1
    return added


def hold_image_success_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    written_keys: list[str] | None = None,
    saved_count: int | None = None,
    params_update: dict | None = None,
) -> None:
    """Hold provider-success image tasks when local accounting failed."""
    db.rollback()
    task = db.get(GenTask, task_id)
    if not task:
        return
    params = {**(task.params or {}), **(params_update or {})}
    if written_keys:
        params["_image_result_keys"] = list(written_keys)
    if saved_count is not None:
        params["_saved_n"] = int(saved_count)
    persist_local_image_result_assets(db, task, written_keys)
    mark_needs_review(
        db,
        task_id,
        (
            "图片已由上游生成,但本地落账失败,需要系统恢复或管理员确认。"
            f"saved_n={saved_count if saved_count is not None else 'unknown'}; "
            f"error={error[:500]}"
        ),
        params_update=params,
        rollback=False,
    )


def hold_image_submit_unknown_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    written_keys: list[str] | None = None,
    saved_count: int | None = None,
    requested_count: int | None = None,
    params_update: dict | None = None,
) -> None:
    """Hold image batches when at least one upstream submit may still finish."""
    db.rollback()
    task = db.get(GenTask, task_id)
    if not task:
        return
    params = {**(task.params or {}), **(params_update or {})}
    if written_keys:
        params["_image_result_keys"] = list(written_keys)
    if saved_count is not None:
        params["_saved_n"] = int(saved_count)
    if requested_count is not None:
        params["_requested_n"] = int(requested_count)
    if requested_count is not None or saved_count is not None:
        requested = int(requested_count or 0)
        saved = int(saved_count or 0)
        params["_partial"] = True
        params["_skipped_n"] = max(0, requested - saved)
        params["_partial_errors"] = [error[:160]] if error else []
    params["_image_submit_state_unknown"] = True
    persist_local_image_result_assets(db, task, written_keys)
    mark_needs_review(
        db,
        task_id,
        (
            "图片批量生成存在上游提交状态未知的子请求,已生成的结果已本地保存,"
            "冻结积分暂不结算或退回,需要系统恢复或管理员确认。"
            f"saved_n={saved_count if saved_count is not None else 'unknown'}; "
            f"requested_n={requested_count if requested_count is not None else 'unknown'}; "
            f"error={error[:500]}"
        ),
        params_update=params,
        rollback=False,
    )


def public_image_error(exc: Exception) -> str:
    if isinstance(exc, ReviewedEvidenceMaskError):
        return f"已确认的图片证据蒙版无法安全应用，已停止生成并退回冻结积分：{str(exc)[:160]}"
    if isinstance(exc, ProductProtectionUnavailable):
        return (
            "产品主体保护未能可靠识别或应用蒙版，已停止本次生成并退回冻结积分。"
            "请稍后重试，或重新上传主体清晰、背景对比明显的产品图。"
        )
    message = str(exc)
    lowered = message.lower()
    if "timed out" in lowered or "timeout" in lowered or "超时" in message:
        return (
            "图片生成等待超时，已退回冻结积分。大图或高峰期可能需要更久；"
            "请稍后重试，或先降低分辨率/张数。"
        )
    if "没有可用账号" in message or "No available compatible accounts" in message:
        return (
            "图片网关当前没有可用账号支持该模型/参数组合，已退回冻结积分。"
            "请稍后重试或在后台切换图像模型/网关账号。"
        )
    if "images[].image_url" in message or "unknown parameter" in lowered or "invalid_request_error" in lowered:
        return (
            "图片网关参数不匹配，已退回冻结积分。"
            "请检查后台图像模型配置或联系管理员处理。"
        )
    return "图片生成失败，已退回冻结积分，请稍后重试"


def image_submit_state_unknown(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return False
    if isinstance(exc, gateway.GatewayError):
        return bool(getattr(exc, "submit_state_unknown", False))
    return False


def _claim_image_task(db, task_id: int) -> GenTask | None:
    task = db.get(GenTask, task_id)
    if not task or task.status in TERMINAL_STATUSES:
        return None
    raise_if_cancel_requested(db, task)
    claim_params = dict(task.params or {})
    claim_params["_image_render_started_at"] = datetime.now(timezone.utc).isoformat()
    claimed = db.execute(
        update(GenTask)
        .where(GenTask.id == task_id, GenTask.status == "queued")
        .ordered_values(
            (GenTask.status, "running"),
            (GenTask.phase, "rendering"),
            (GenTask.params, claim_params),
        )
    ).rowcount
    if (claimed or 0) != 1:
        db.rollback()
        return None
    db.commit()
    task = db.get(GenTask, task_id)
    if not task:
        return None
    raise_if_cancel_requested(db, task)
    set_progress(task_id, 10, "running")
    return task


def _prepare_image_request(db, task_id: int, task: GenTask) -> _ImageRequest:
    model = model_config_for_task(db, task, "image", get_model_config)
    if not model or not model.enabled:
        raise RuntimeError("未配置可用的图像模型")
    model = model_from_snapshot(task, model, db)

    n = int((task.params or {}).get("n") or get_setting(db, "image_n", 1))
    params = task.params or {}
    is_product = is_product_generation_task(task)
    is_portrait = is_portrait_generation_task(task)
    fallback_size = get_setting(db, "image_size", "1024x1024")
    ref_w, ref_h = reference_dimensions(task)
    size = params.get("size") or closest_image_size(ref_w, ref_h, fallback_size)
    prompt = final_prompt(task)
    prompt = generation_prompt_for_model(prompt, task)
    prompt = product_fidelity_prompt(prompt, task)
    params["_generation_prompt"] = prompt
    task.params = dict(params)
    db.commit()

    set_progress(task_id, 30, "running")
    raise_if_cancel_requested(db, db.get(GenTask, task_id))
    return _ImageRequest(
        task=task,
        model=model,
        n=n,
        params=params,
        is_product=is_product,
        is_portrait=is_portrait,
        size=size,
        prompt=prompt,
    )


def _prepare_image_references(
    db,
    task_id: int,
    request: _ImageRequest,
) -> _ImageRenderPlan:
    task = request.task
    params = request.params
    ref = None
    ref_content_hash = None
    edit_refs: list[str] | None = None
    reference_max_side = (
        IMAGE_PRODUCT_EDIT_REFERENCE_MAX_SIDE
        if request.is_product
        else (
            IMAGE_PORTRAIT_EDIT_REFERENCE_MAX_SIDE
            if request.is_portrait
            else IMAGE_EDIT_REFERENCE_MAX_SIDE
        )
    )
    explicit_reference_url = (
        params.get("reference_image_url")
        or params.get("character_reference_image")
    )
    edit_source_url = explicit_reference_url or task.source_asset_url
    should_load_reference = bool(explicit_reference_url) or (
        bool((task.prompt or {}).get("instruction")) and task.source_type == "image"
    )
    if should_load_reference:
        resolved_ref = gateway_reference_image(
            db,
            task,
            explicit_reference_url or task.source_asset_url,
            max_side=reference_max_side,
            prefer_original_upload=True,
            quality=92,
            subsampling=0,
            return_content_hash=True,
        )
        if isinstance(resolved_ref, tuple):
            ref, ref_content_hash = resolved_ref
        else:
            ref = resolved_ref
        edit_refs = [ref] if ref else None
        style_reference_url = str(params.get("style_reference_image") or "").strip()
        multi_image_setting = (request.model.extra or {}).get("multi_image_edit_enabled")
        multi_image_disabled = multi_image_setting is False or str(
            multi_image_setting or ""
        ).strip().lower() in {"0", "false", "off", "no"}
        style_reference_error = ""
        if ref and style_reference_url and not multi_image_disabled:
            try:
                style_ref = gateway_reference_image(
                    db,
                    task,
                    style_reference_url,
                    max_side=IMAGE_STYLE_REFERENCE_MAX_SIDE,
                    prefer_original_upload=True,
                    quality=92,
                    subsampling=0,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("image task %s style reference skipped: %s", task_id, e)
                style_reference_error = "invalid_or_unavailable"
                style_ref = None
            if style_ref:
                edit_refs = [ref, style_ref]
        reference_count = len(edit_refs or [])
        style_reference_sent = reference_count >= 2
        style_reference_skip_reason = ""
        if style_reference_url and not style_reference_sent:
            if multi_image_disabled:
                style_reference_skip_reason = "model_disabled"
            elif not ref:
                style_reference_skip_reason = "primary_reference_unavailable"
            else:
                style_reference_skip_reason = style_reference_error or "unavailable"
        task.params = {
            **(task.params or {}),
            "_style_reference_requested": bool(style_reference_url),
            "_style_reference_sent": style_reference_sent,
            "_reference_image_count": reference_count,
            **(
                {"_style_reference_skip_reason": style_reference_skip_reason}
                if style_reference_skip_reason
                else {}
            ),
        }
        db.commit()
    edit_path = (request.model.extra or {}).get("edit_path", settings.image_edit_path) or None
    if ref and not edit_path:
        raise RuntimeError("参考图生成需要配置图片编辑接口")
    extra_payload = {
        "seed": params.get("seed"),
        "negative_prompt": (
            product_image_negative_prompt(params.get("negative_prompt") or params.get("negative"))
            if request.is_product
            else (
                portrait_image_negative_prompt(
                    params.get("negative_prompt") or params.get("negative"),
                    portrait_negative_prompt_evidence(task),
                )
                if request.is_portrait
                else (params.get("negative_prompt") or params.get("negative"))
            )
        ),
        "edit_payload_format": (request.model.extra or {}).get("edit_payload_format"),
    }
    return _ImageRenderPlan(
        ref=ref,
        ref_content_hash=ref_content_hash,
        edit_refs=edit_refs,
        reference_max_side=reference_max_side,
        edit_source_url=edit_source_url,
        edit_path=edit_path,
        extra_payload=extra_payload,
    )


def _prepare_reviewed_image_mask(
    db,
    request: _ImageRequest,
    plan: _ImageRenderPlan,
    reproduction_context: dict | None,
) -> tuple[Any, Any]:
    task = request.task
    mask_result = None
    reviewed_plan = None
    if reproduction_context is not None:
        from . import reproduction_remediation

        if not plan.ref:
            raise ReviewedEvidenceMaskError("复刻纠偏局部重绘需要可用的编辑源图")
        if not plan.edit_path:
            raise ReviewedEvidenceMaskError("所选图片模型不支持局部重绘")
        mask_result = reproduction_remediation.rasterize_image_remediation_mask(
            db,
            task=task,
            reference_data_uri=plan.ref,
            reference_content_hash=plan.ref_content_hash,
        )
        plan.extra_payload["mask"] = mask_result.data_uri
        task.params = {
            **(task.params or {}),
            **mask_result.metadata,
            "_edit_mask_requested_mode": "reproduction_remediation",
            "_edit_mask_sent": True,
        }
        reproduction_remediation.attach_image_remediation_mask_metadata(
            db,
            task=task,
            mask=mask_result,
            commit=False,
        )
        db.commit()
        return mask_result, reviewed_plan

    reviewed_plan = resolve_reviewed_evidence_plan(
        db,
        task,
        edit_source_url=plan.edit_source_url,
    )
    if reviewed_plan is None:
        return mask_result, reviewed_plan
    if not plan.ref:
        raise ReviewedEvidenceMaskError("已确认证据蒙版需要可用的编辑源图")
    if not plan.edit_path:
        raise ReviewedEvidenceMaskError("所选图片模型不支持蒙版编辑")
    mask_result = rasterize_reviewed_evidence_mask(
        reviewed_plan,
        reference_data_uri=plan.ref,
        reference_content_hash=plan.ref_content_hash,
    )
    plan.extra_payload["mask"] = mask_result.data_uri
    task.params = {
        **(task.params or {}),
        **mask_result.metadata,
        "_edit_mask_requested_mode": "reviewed_evidence",
        "_edit_mask_sent": True,
    }
    attach_mask_metadata_to_generation_revision(db, task, mask_result)
    db.commit()
    return mask_result, reviewed_plan


def _prepare_image_mask(
    db,
    task_id: int,
    request: _ImageRequest,
    plan: _ImageRenderPlan,
) -> None:
    task = request.task
    params = request.params
    edit_mask_mode = str(params.get("edit_mask_mode") or "").lower().strip()
    product_pixel_lock_mode = str(params.get("product_pixel_lock") or "auto").lower().strip()
    reproduction_context = (
        dict(params.get("_reproduction_context") or {})
        if isinstance(params.get("_reproduction_context"), dict)
        else None
    )
    mask_result, reviewed_plan = _prepare_reviewed_image_mask(
        db,
        request,
        plan,
        reproduction_context,
    )
    product_pixel_lock_label = "strict"
    product_pixel_lock = bool(
        request.is_product
        and plan.ref
        and plan.edit_path
        and (
            reproduction_context is not None
            or reviewed_plan is not None
            or edit_mask_mode != "off"
        )
        and product_pixel_lock_mode not in {"off", "false", "0"}
    )
    if reproduction_context is not None:
        product_pixel_lock_label = "reproduction_remediation"
    elif reviewed_plan is not None:
        if product_pixel_lock_mode in {"strict", "on", "true", "1"}:
            product_pixel_lock_label = "strict"
        else:
            product_pixel_lock_label = "reviewed_evidence"
    elif request.is_product and plan.ref and plan.edit_path and edit_mask_mode != "off":
        mask_source = params.get("mask_image_url") or task.source_asset_url
        mask_failure_source = ""
        for mask_attempt in range(2):
            try:
                mask_result = gateway_image_edit_mask(
                    db,
                    task,
                    mask_source,
                    max_side=plan.reference_max_side,
                    edit_mask_mode=edit_mask_mode or "protect_subject",
                )
                break
            except SubjectProtectionBusy as e:
                if mask_attempt == 0:
                    log.info("image task %s edit mask busy; retrying once", task_id)
                    time.sleep(IMAGE_EDIT_MASK_RETRY_DELAY_SECONDS)
                    continue
                mask_failure_source = "generation_mask_busy"
                log.warning("image task %s edit mask skipped after retry: %s", task_id, e)
            except Exception as e:  # noqa: BLE001
                if mask_attempt == 0:
                    log.info("image task %s edit mask failed; retrying once: %s", task_id, e)
                    time.sleep(IMAGE_EDIT_MASK_RETRY_DELAY_SECONDS)
                    continue
                mask_failure_source = "generation_mask_error"
                log.warning("image task %s edit mask skipped after retry: %s", task_id, e)
            mask_result = None
            break
        if mask_result:
            should_send_mask = bool(mask_result.data_uri) and (
                (
                    mask_result.mode in {"alpha_subject", "auto_subject"}
                    and mask_result.confidence >= EDIT_MASK_SEND_CONFIDENCE
                )
                or (
                    edit_mask_mode == "center_box"
                    and mask_result.mode == "center_box"
                )
            )
            task.params = {
                **(task.params or {}),
                **mask_result.metadata,
                "_edit_mask_requested_mode": edit_mask_mode or "protect_subject",
                "_edit_mask_sent": should_send_mask,
            }
            db.commit()
            if should_send_mask:
                plan.extra_payload["mask"] = mask_result.data_uri
            else:
                raise ProductProtectionUnavailable("产品主体保护置信度不足，未向图片网关发送蒙版")
            if product_pixel_lock_mode in {"strict", "on", "true", "1"}:
                product_pixel_lock_label = "strict"
                product_pixel_lock = product_pixel_lock and should_send_mask
            else:
                product_pixel_lock_label = (
                    "auto_alpha"
                    if mask_result.mode == "alpha_subject"
                    else "auto_mask_only"
                )
                product_pixel_lock = (
                    product_pixel_lock
                    and should_send_mask
                    and mask_result.mode == "alpha_subject"
                )
        else:
            task.params = {
                **(task.params or {}),
                "_edit_mask_mode": "none",
                "_edit_mask_confidence": 0.0,
                "_edit_mask_bbox": None,
                "_edit_mask_source": mask_failure_source or "generation_mask_unavailable",
                "_edit_mask_requested_mode": edit_mask_mode or "protect_subject",
                "_edit_mask_sent": False,
            }
            db.commit()
            raise ProductProtectionUnavailable(
                "产品主体保护蒙版生成失败："
                f"{mask_failure_source or 'generation_mask_unavailable'}"
            )
    elif product_pixel_lock:
        product_pixel_lock = False
    if product_pixel_lock and not mask_result:
        product_pixel_lock = False
    plan.mask_result = mask_result
    plan.product_pixel_lock = product_pixel_lock
    plan.product_pixel_lock_label = product_pixel_lock_label
    plan.mode = "edit" if (plan.ref and plan.edit_path) else "txt2img"


def _invoke_image_gateway(
    db,
    task_id: int,
    request: _ImageRequest,
    plan: _ImageRenderPlan,
    *,
    gen_image_fn=None,
) -> _ImageGatewayOutcome | None:
    started_at = time.time()
    try:
        image_generator = gen_image_fn or gen_image_with_model_config
        images = image_generator(
            request.model,
            request.prompt,
            n=request.n,
            size=request.size,
            reference_image_url=plan.ref,
            reference_image_urls=plan.edit_refs,
            edit_path=plan.edit_path,
            extra_payload=plan.extra_payload,
        )
        if not images:
            raise RuntimeError("图片网关未返回任何结果")
    except Exception as e:  # noqa: BLE001
        usage.record_call(
            db,
            kind="image",
            model_id=request.model.model_id,
            user_id=request.task.user_id,
            task_id=request.task.id,
            status="failed",
            latency_ms=int((time.time() - started_at) * 1000),
            detail={
                "n": request.n,
                "size": request.size,
                "mode": plan.mode,
                "error": str(e)[:300],
            },
        )
        if image_submit_state_unknown(e):
            hold_image_submit_unknown_for_reconciliation(
                db,
                task_id,
                str(e),
                written_keys=[],
                saved_count=0,
                requested_count=request.n,
            )
            return None
        raise

    failure_items = list(getattr(images, "failures", []) or [])
    failures = [
        failure.message
        for failure in failure_items
        if getattr(failure, "message", "")
    ]
    diagnostics = list(getattr(images, "diagnostics", []) or [])
    return _ImageGatewayOutcome(
        images=images,
        failure_items=failure_items,
        failures=failures,
        has_unknown_failure=any(
            bool(getattr(failure, "submit_state_unknown", False))
            for failure in failure_items
        ),
        echo_sizes=[
            str(item.size)
            for item in diagnostics
            if getattr(item, "size", None)
        ],
        echo_qualities=[
            str(item.quality)
            for item in diagnostics
            if getattr(item, "quality", None)
        ],
        echo_formats=[
            str(item.output_format)
            for item in diagnostics
            if getattr(item, "output_format", None)
        ],
        echo_models=[
            str(item.model)
            for item in diagnostics
            if getattr(item, "model", None)
        ],
        selected_sources=[
            str(item.selected_source)
            for item in diagnostics
            if getattr(item, "selected_source", None)
        ],
        latency_ms=int((time.time() - started_at) * 1000),
    )


def _persist_image_results(
    db,
    task_id: int,
    request: _ImageRequest,
    plan: _ImageRenderPlan,
    gateway_outcome: _ImageGatewayOutcome,
) -> _ImagePersistenceOutcome:
    set_progress(task_id, 70, "running")
    raise_if_cancel_requested(db, db.get(GenTask, task_id))
    written_keys: list[str] = []
    image_errors: list[str] = []
    actual_sizes: list[str] = []
    returned_sizes: list[str] = []
    product_composite_count = 0
    product_composite_meta: dict | None = None
    pending_assets: list[GenAsset] = []

    for raw_image in gateway_outcome.images:
        item_keys: list[str] = []
        try:
            raw = raw_image
            if plan.product_pixel_lock and plan.mask_result:
                try:
                    composited = composite_product_subject_pixels(
                        db,
                        request.task,
                        raw,
                        plan.edit_source_url,
                        plan.mask_result,
                        max_side=plan.reference_max_side,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("image task %s product pixel lock skipped: %s", task_id, e)
                    composited = None
                if composited:
                    raw, product_composite_meta = composited
                    product_composite_count += 1
            assert_generated_media_allowed(
                db,
                task_id=request.task.id,
                user_id=request.task.user_id,
                media_type="image",
                data=raw,
                mime_type=f"image/{image_ext(raw)}",
            )
            preview_png, hd_w, hd_h = make_image_preview(
                raw,
                max_pixels=int(settings.generated_image_max_pixels),
            )
            returned_sizes.append(f"{hd_w}x{hd_h}")
            model_ref_jpeg, _, _ = make_model_reference(
                raw,
                max_pixels=int(settings.generated_image_max_pixels),
            )
            hd_key = storage.save_bytes(raw, "hd", image_ext(raw))
            item_keys.append(hd_key)
            preview_key = storage.save_bytes(preview_png, "preview", "png")
            item_keys.append(preview_key)
            model_ref_key = storage.save_bytes_named(
                model_ref_jpeg,
                "model_ref",
                preview_key.split("/", 1)[1].rsplit(".", 1)[0] + ".jpg",
            )
            item_keys.append(model_ref_key)
            written_keys.extend(item_keys)
            pending_assets.append(
                GenAsset(
                    task_id=request.task.id,
                    user_id=request.task.user_id,
                    type="image",
                    preview_url=storage.public_url(preview_key),
                    hd_url=storage.public_url(hd_key),
                    watermarked=False,
                    unlocked=True,
                    width=hd_w,
                    height=hd_h,
                    bytes=storage_bytes_for_keys(item_keys),
                )
            )
            actual_sizes.append(f"{hd_w}x{hd_h}")
        except Exception as e:  # noqa: BLE001
            unlink_keys(item_keys)
            image_errors.append(str(e)[:300])
            log.warning("image task %s skipped one invalid image: %s", task_id, e)

    pixel_lock_params_update = (
        {
            "_product_pixel_lock": plan.product_pixel_lock_label,
            "_product_composite_applied_count": product_composite_count,
            **(product_composite_meta or {}),
        }
        if plan.product_pixel_lock
        else {}
    )
    return _ImagePersistenceOutcome(
        written_keys=written_keys,
        saved_count=len(pending_assets),
        image_errors=image_errors,
        actual_sizes=actual_sizes,
        returned_sizes=returned_sizes,
        pending_assets=pending_assets,
        pixel_lock_params_update=pixel_lock_params_update,
    )


def _image_usage_detail(
    request: _ImageRequest,
    plan: _ImageRenderPlan,
    gateway_outcome: _ImageGatewayOutcome,
    persisted: _ImagePersistenceOutcome,
    *,
    errors: list[str],
    include_actual_sizes: bool,
) -> dict:
    return {
        "n": request.n,
        "returned_n": len(gateway_outcome.images),
        "saved_n": persisted.saved_count,
        "size": request.size,
        "mode": plan.mode,
        **(
            {"returned_sizes": persisted.returned_sizes[:10]}
            if persisted.returned_sizes
            else {}
        ),
        **(
            {"actual_sizes": persisted.actual_sizes[:10]}
            if include_actual_sizes and persisted.actual_sizes
            else {}
        ),
        **({"gateway_echo_sizes": gateway_outcome.echo_sizes[:10]} if gateway_outcome.echo_sizes else {}),
        **(
            {"gateway_echo_qualities": gateway_outcome.echo_qualities[:10]}
            if gateway_outcome.echo_qualities
            else {}
        ),
        **(
            {"gateway_echo_formats": gateway_outcome.echo_formats[:10]}
            if gateway_outcome.echo_formats
            else {}
        ),
        **({"gateway_echo_models": gateway_outcome.echo_models[:5]} if gateway_outcome.echo_models else {}),
        **(
            {"gateway_selected_sources": gateway_outcome.selected_sources[:10]}
            if gateway_outcome.selected_sources
            else {}
        ),
        **({"errors": errors[:5]} if errors else {}),
    }


def _record_image_generation_usage(
    db,
    request: _ImageRequest,
    plan: _ImageRenderPlan,
    gateway_outcome: _ImageGatewayOutcome,
    persisted: _ImagePersistenceOutcome,
) -> None:
    if persisted.saved_count <= 0:
        errors = gateway_outcome.failures + persisted.image_errors
        usage.record_call(
            db,
            kind="image",
            model_id=request.model.model_id,
            user_id=request.task.user_id,
            task_id=request.task.id,
            status="failed",
            latency_ms=gateway_outcome.latency_ms,
            detail=_image_usage_detail(
                request,
                plan,
                gateway_outcome,
                persisted,
                errors=errors,
                include_actual_sizes=False,
            ),
        )
        raise RuntimeError(
            persisted.image_errors[0]
            if persisted.image_errors
            else "图片网关返回结果均无法解析"
        )
    usage.record_call(
        db,
        kind="image",
        model_id=request.model.model_id,
        user_id=request.task.user_id,
        task_id=request.task.id,
        status="ok",
        latency_ms=gateway_outcome.latency_ms,
        detail=_image_usage_detail(
            request,
            plan,
            gateway_outcome,
            persisted,
            errors=gateway_outcome.failures,
            include_actual_sizes=True,
        ),
    )


def _settle_image_generation(
    db,
    task_id: int,
    request: _ImageRequest,
    gateway_outcome: _ImageGatewayOutcome,
    persisted: _ImagePersistenceOutcome,
) -> tuple[bool, dict | None]:
    real_cost = settlement_cost(
        request.task,
        request.model,
        image_count=persisted.saved_count,
    )
    skipped_n = max(0, request.n - persisted.saved_count)
    partial_errors = (
        gateway_outcome.failures
        if persisted.saved_count < request.n
        else []
    ) + persisted.image_errors
    unknown_errors = [
        failure.message
        for failure in gateway_outcome.failure_items
        if getattr(failure, "submit_state_unknown", False)
        and getattr(failure, "message", "")
    ]
    partial_detail = (
        {
            "requested_n": request.n,
            "saved_n": persisted.saved_count,
            "skipped_n": skipped_n,
            "errors": partial_errors[:5],
            **({"actual_sizes": persisted.actual_sizes[:5]} if persisted.actual_sizes else {}),
            **({"submit_state_unknown": True} if gateway_outcome.has_unknown_failure else {}),
        }
        if persisted.saved_count < request.n or persisted.image_errors
        else None
    )
    if gateway_outcome.has_unknown_failure:
        hold_image_submit_unknown_for_reconciliation(
            db,
            task_id,
            "; ".join(unknown_errors or partial_errors or ["unknown image submit state"])[:500],
            written_keys=persisted.written_keys,
            saved_count=persisted.saved_count,
            requested_count=request.n,
            params_update=persisted.pixel_lock_params_update,
        )
        return False, partial_detail
    try:
        terminal_task = acquire_image_terminal_boundary(db, task_id)
        if not terminal_task:
            db.rollback()
            unlink_keys(persisted.written_keys)
            return False, partial_detail
        raise_if_cancel_requested(db, terminal_task)
        final_params = dict(terminal_task.params or {})
        final_params.update(persisted.pixel_lock_params_update)
        if partial_detail:
            final_params.update(
                {
                    "_partial": True,
                    "_requested_n": request.n,
                    "_saved_n": persisted.saved_count,
                    "_skipped_n": skipped_n,
                    "_partial_errors": partial_errors[:5],
                    "_image_result_keys": list(persisted.written_keys),
                    **(
                        {"_image_submit_state_unknown": True}
                        if gateway_outcome.has_unknown_failure
                        else {}
                    ),
                    **({"_unknown_submit_errors": unknown_errors[:5]} if unknown_errors else {}),
                }
            )
        elif persisted.actual_sizes:
            final_params["_actual_sizes"] = persisted.actual_sizes[:20]
        terminal_task.params = final_params
        db.add_all(persisted.pending_assets)

        if not claim_terminal(db, task_id, "succeeded", cost_settled=real_cost):
            db.rollback()
            unlink_keys(persisted.written_keys)
            return False, partial_detail
        credits.settle(
            db,
            request.task.user_id,
            reserved=request.task.cost_frozen,
            real_cost=real_cost,
            biz_ref=request.task.id,
            commit=False,
        )
        db.commit()
    except TaskCanceled:
        db.rollback()
        unlink_keys(persisted.written_keys)
        raise
    except Exception as e:  # noqa: BLE001
        hold_image_success_for_reconciliation(
            db,
            task_id,
            f"图片本地落账失败:{e}",
            written_keys=persisted.written_keys,
            saved_count=persisted.saved_count,
            params_update=persisted.pixel_lock_params_update,
        )
        raise
    return True, partial_detail


def _execute_image_task(db, task_id: int, *, gen_image_fn=None) -> None:
    task = _claim_image_task(db, task_id)
    if not task:
        return
    request = _prepare_image_request(db, task_id, task)
    plan = _prepare_image_references(db, task_id, request)
    _prepare_image_mask(db, task_id, request, plan)
    gateway_outcome = _invoke_image_gateway(
        db,
        task_id,
        request,
        plan,
        gen_image_fn=gen_image_fn,
    )
    if gateway_outcome is None:
        return
    persisted = _persist_image_results(db, task_id, request, plan, gateway_outcome)
    _record_image_generation_usage(db, request, plan, gateway_outcome, persisted)
    settled, partial_detail = _settle_image_generation(
        db,
        task_id,
        request,
        gateway_outcome,
        persisted,
    )
    if not settled:
        return
    if partial_detail:
        usage.record_call(
            db,
            kind="image",
            model_id=request.model.model_id,
            user_id=request.task.user_id,
            task_id=request.task.id,
            status="ok",
            detail=partial_detail,
        )
    set_progress(task_id, 100, "succeeded")
    publish_task_update(request.task, "succeeded")


def run_image_task(task_id: int, *, gen_image_fn=None) -> None:
    lock_key = f"gen:lock:{task_id}"
    lock_token = locks.acquire(lock_key)
    if not lock_token:
        log.warning("image task %s locked by another worker, retry later", task_id)
        raise TaskLockedError(f"image task {task_id} locked")
    db = SessionLocal()
    try:
        _execute_image_task(db, task_id, gen_image_fn=gen_image_fn)
    except TaskCanceled as e:
        cancel_and_refund(db, task_id, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("image task %s failed", task_id)
        current = db.get(GenTask, task_id)
        if current and current.status == NEEDS_REVIEW:
            return
        fail_and_refund(db, task_id, str(e), public_error=public_image_error(e))
    finally:
        db.close()
        locks.release(lock_key, lock_token)
