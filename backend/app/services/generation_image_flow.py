"""Image generation task flow."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, update

from ..config import settings
from ..db import SessionLocal
from ..models import GenAsset, GenTask
from . import credits, gateway, locks, storage, usage
from .config_store import get_model_config, get_setting
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
from .generation_media import (
    EDIT_MASK_SEND_CONFIDENCE,
    closest_image_size,
    composite_product_subject_pixels,
    final_prompt,
    gateway_image_edit_mask,
    gateway_reference_image,
    reference_dimensions,
)
from .generation_model_runtime import gen_image_with_model_config, model_from_snapshot
from .generation_prompts import (
    generation_prompt_for_model,
    is_portrait_generation_task,
    is_product_generation_task,
    portrait_image_negative_prompt,
    product_fidelity_prompt,
    product_image_negative_prompt,
)
from .generation_state import NEEDS_REVIEW, claim_terminal
from .generation_state import TERMINAL_STATUSES as TERMINAL_STATUSES
from .generation_video_flow import unlink_keys
from .progress import set_progress
from .watermark import dimensions, image_ext, make_image_preview, make_model_reference

log = logging.getLogger("generation")

IMAGE_EDIT_REFERENCE_MAX_SIDE = 1024
IMAGE_PRODUCT_EDIT_REFERENCE_MAX_SIDE = 1536
IMAGE_PORTRAIT_EDIT_REFERENCE_MAX_SIDE = 1536
PORTRAIT_NEGATIVE_EVIDENCE_KEYS = (
    "final_text",
    "主体",
    "人物比例",
    "身材体态",
    "体态线条",
    "服装结构",
    "妆发五官",
    "视角镜头",
    "光线",
    "后期质感",
)


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
        return all(storage.local_path(key).exists() for key in [*hd_keys, *preview_keys])
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


def run_image_task(task_id: int, *, gen_image_fn=None) -> None:
    lock_key = f"gen:lock:{task_id}"
    lock_token = locks.acquire(lock_key)
    if not lock_token:
        log.warning("image task %s locked by another worker, retry later", task_id)
        raise TaskLockedError(f"image task {task_id} locked")
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in TERMINAL_STATUSES:
            return
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
            return
        db.commit()
        task = db.get(GenTask, task_id)
        if not task:
            return
        raise_if_cancel_requested(db, task)
        set_progress(task_id, 10, "running")

        model = get_model_config(db, "image")
        if not model or not model.enabled:
            raise RuntimeError("未配置可用的图像模型")
        model = model_from_snapshot(task, model)

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
        prompt_obj = task.prompt if isinstance(task.prompt, dict) else {}
        portrait_negative_evidence = "；".join(
            str(prompt_obj.get(key) or "")
            for key in PORTRAIT_NEGATIVE_EVIDENCE_KEYS
            if prompt_obj.get(key)
        )

        set_progress(task_id, 30, "running")
        raise_if_cancel_requested(db, db.get(GenTask, task_id))
        ref = None
        edit_refs: list[str] | None = None
        reference_max_side = (
            IMAGE_PRODUCT_EDIT_REFERENCE_MAX_SIDE
            if is_product
            else (
                IMAGE_PORTRAIT_EDIT_REFERENCE_MAX_SIDE
                if is_portrait
                else IMAGE_EDIT_REFERENCE_MAX_SIDE
            )
        )
        explicit_reference_url = params.get("reference_image_url")
        should_load_reference = bool(explicit_reference_url) or (
            bool((task.prompt or {}).get("instruction")) and task.source_type == "image"
        )
        if should_load_reference:
            ref = gateway_reference_image(
                db,
                task,
                explicit_reference_url or task.source_asset_url,
                max_side=reference_max_side,
                prefer_original_upload=True,
                quality=92,
                subsampling=0,
            )
            edit_refs = [ref] if ref else None
            if ref and (model.extra or {}).get("multi_image_edit_enabled"):
                try:
                    style_ref = gateway_reference_image(
                        db,
                        task,
                        params.get("style_reference_image"),
                        max_side=384,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("image task %s style reference skipped: %s", task_id, e)
                    style_ref = None
                if style_ref:
                    edit_refs = [ref, style_ref]
        edit_path = (model.extra or {}).get("edit_path", settings.image_edit_path) or None
        if ref and is_portrait and not edit_path:
            raise RuntimeError("人物参考图生成需要配置图片编辑接口")
        extra_payload = {
            "seed": params.get("seed"),
            "negative_prompt": (
                product_image_negative_prompt(params.get("negative_prompt") or params.get("negative"))
                if is_product
                else (
                    portrait_image_negative_prompt(
                        params.get("negative_prompt") or params.get("negative"),
                        portrait_negative_evidence,
                    )
                    if is_portrait
                    else (params.get("negative_prompt") or params.get("negative"))
                )
            ),
            "edit_payload_format": (model.extra or {}).get("edit_payload_format"),
        }
        edit_mask_mode = str(params.get("edit_mask_mode") or "").lower().strip()
        product_pixel_lock_mode = str(params.get("product_pixel_lock") or "auto").lower().strip()
        mask_result = None
        product_pixel_lock_label = "strict"
        product_pixel_lock = (
            is_product
            and ref
            and edit_path
            and edit_mask_mode != "off"
            and product_pixel_lock_mode not in {"off", "false", "0"}
        )
        if is_product and ref and edit_path and edit_mask_mode != "off":
            mask_source = params.get("mask_image_url") or task.source_asset_url
            try:
                mask_result = gateway_image_edit_mask(
                    db,
                    task,
                    mask_source,
                    max_side=reference_max_side,
                    edit_mask_mode=edit_mask_mode or "protect_subject",
                )
            except Exception as e:  # noqa: BLE001
                log.warning("image task %s edit mask skipped: %s", task_id, e)
                mask_result = None
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
                    extra_payload["mask"] = mask_result.data_uri
                if product_pixel_lock_mode in {"strict", "on", "true", "1"}:
                    product_pixel_lock_label = "strict"
                    product_pixel_lock = product_pixel_lock and should_send_mask
                else:
                    product_pixel_lock_label = "auto_subject" if mask_result.mode == "auto_subject" else "auto_alpha"
                    product_pixel_lock = (
                        product_pixel_lock
                        and should_send_mask
                        and (
                            mask_result.mode == "alpha_subject"
                            or (
                                mask_result.mode == "auto_subject"
                                and mask_result.confidence >= 0.78
                            )
                        )
                    )
        elif product_pixel_lock:
            product_pixel_lock = False
        if product_pixel_lock and not mask_result:
            product_pixel_lock = False
        mode = "edit" if (ref and edit_path) else "txt2img"
        t0 = time.time()
        try:
            image_generator = gen_image_fn or gen_image_with_model_config
            images = image_generator(
                model,
                prompt,
                n=n,
                size=size,
                reference_image_url=ref,
                reference_image_urls=edit_refs,
                edit_path=edit_path,
                extra_payload=extra_payload,
            )
            if not images:
                raise RuntimeError("图片网关未返回任何结果")
        except Exception as e:  # noqa: BLE001
            usage.record_call(
                db,
                kind="image",
                model_id=model.model_id,
                user_id=task.user_id,
                task_id=task.id,
                status="failed",
                latency_ms=int((time.time() - t0) * 1000),
                detail={"n": n, "size": size, "mode": mode, "error": str(e)[:300]},
            )
            if image_submit_state_unknown(e):
                hold_image_submit_unknown_for_reconciliation(
                    db,
                    task_id,
                    str(e),
                    written_keys=[],
                    saved_count=0,
                    requested_count=n,
                )
                return
            raise
        gateway_failure_items = list(getattr(images, "failures", []) or [])
        has_unknown_gateway_failure = any(
            bool(getattr(failure, "submit_state_unknown", False))
            for failure in gateway_failure_items
        )
        gateway_failures = [
            failure.message
            for failure in gateway_failure_items
            if getattr(failure, "message", "")
        ]
        gateway_diagnostics = list(getattr(images, "diagnostics", []) or [])
        gateway_echo_sizes = [
            str(item.size)
            for item in gateway_diagnostics
            if getattr(item, "size", None)
        ]
        gateway_echo_qualities = [
            str(item.quality)
            for item in gateway_diagnostics
            if getattr(item, "quality", None)
        ]
        gateway_echo_formats = [
            str(item.output_format)
            for item in gateway_diagnostics
            if getattr(item, "output_format", None)
        ]
        gateway_echo_models = [
            str(item.model)
            for item in gateway_diagnostics
            if getattr(item, "model", None)
        ]
        gateway_selected_sources = [
            str(item.selected_source)
            for item in gateway_diagnostics
            if getattr(item, "selected_source", None)
        ]
        gateway_latency_ms = int((time.time() - t0) * 1000)

        set_progress(task_id, 70, "running")
        raise_if_cancel_requested(db, db.get(GenTask, task_id))
        written_keys: list[str] = []
        saved_count = 0
        image_errors: list[str] = []
        actual_sizes: list[str] = []
        returned_sizes: list[str] = []
        product_composite_count = 0
        product_composite_meta: dict | None = None
        pending_assets: list[GenAsset] = []
        for raw in images:
            item_keys: list[str] = []
            try:
                if product_pixel_lock and mask_result:
                    try:
                        composited = composite_product_subject_pixels(
                            db,
                            task,
                            raw,
                            params.get("mask_image_url") or task.source_asset_url,
                            mask_result,
                            max_side=reference_max_side,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("image task %s product pixel lock skipped: %s", task_id, e)
                        composited = None
                    if composited:
                        raw, product_composite_meta = composited
                        product_composite_count += 1
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
                pv_key = storage.save_bytes(preview_png, "preview", "png")
                item_keys.append(pv_key)
                model_ref_key = storage.save_bytes_named(
                    model_ref_jpeg,
                    "model_ref",
                    pv_key.split("/", 1)[1].rsplit(".", 1)[0] + ".jpg",
                )
                item_keys.append(model_ref_key)
                written_keys.extend(item_keys)
                pending_assets.append(
                    GenAsset(
                        task_id=task.id,
                        user_id=task.user_id,
                        type="image",
                        preview_url=storage.public_url(pv_key),
                        hd_url=storage.public_url(hd_key),
                        watermarked=False,
                        unlocked=True,
                        width=hd_w,
                        height=hd_h,
                    )
                )
                saved_count += 1
                actual_sizes.append(f"{hd_w}x{hd_h}")
            except Exception as e:  # noqa: BLE001
                unlink_keys(item_keys)
                image_errors.append(str(e)[:300])
                log.warning("image task %s skipped one invalid image: %s", task_id, e)
        pixel_lock_params_update = (
            {
                "_product_pixel_lock": product_pixel_lock_label,
                "_product_composite_applied_count": product_composite_count,
                **(product_composite_meta or {}),
            }
            if product_pixel_lock
            else {}
        )
        if saved_count <= 0:
            usage.record_call(
                db,
                kind="image",
                model_id=model.model_id,
                user_id=task.user_id,
                task_id=task.id,
                status="failed",
                latency_ms=gateway_latency_ms,
                detail={
                    "n": n,
                    "returned_n": len(images),
                    "saved_n": 0,
                    "size": size,
                    "mode": mode,
                    **({"returned_sizes": returned_sizes[:10]} if returned_sizes else {}),
                    **({"gateway_echo_sizes": gateway_echo_sizes[:10]} if gateway_echo_sizes else {}),
                    **({"gateway_echo_qualities": gateway_echo_qualities[:10]} if gateway_echo_qualities else {}),
                    **({"gateway_echo_formats": gateway_echo_formats[:10]} if gateway_echo_formats else {}),
                    **({"gateway_echo_models": gateway_echo_models[:5]} if gateway_echo_models else {}),
                    **({"gateway_selected_sources": gateway_selected_sources[:10]} if gateway_selected_sources else {}),
                    **({"errors": (gateway_failures + image_errors)[:5]} if gateway_failures or image_errors else {}),
                },
            )
            raise RuntimeError(image_errors[0] if image_errors else "图片网关返回结果均无法解析")
        usage.record_call(
            db,
            kind="image",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="ok",
            latency_ms=gateway_latency_ms,
            detail={
                "n": n,
                "returned_n": len(images),
                "saved_n": saved_count,
                "size": size,
                "mode": mode,
                **({"returned_sizes": returned_sizes[:10]} if returned_sizes else {}),
                **({"actual_sizes": actual_sizes[:10]} if actual_sizes else {}),
                **({"gateway_echo_sizes": gateway_echo_sizes[:10]} if gateway_echo_sizes else {}),
                **({"gateway_echo_qualities": gateway_echo_qualities[:10]} if gateway_echo_qualities else {}),
                **({"gateway_echo_formats": gateway_echo_formats[:10]} if gateway_echo_formats else {}),
                **({"gateway_echo_models": gateway_echo_models[:5]} if gateway_echo_models else {}),
                **({"gateway_selected_sources": gateway_selected_sources[:10]} if gateway_selected_sources else {}),
                **({"errors": gateway_failures[:5]} if gateway_failures else {}),
            },
        )
        real_cost = settlement_cost(task, model, image_count=saved_count)
        skipped_n = max(0, n - saved_count)
        partial_errors = (gateway_failures if saved_count < n else []) + image_errors
        unknown_errors = [
            failure.message
            for failure in gateway_failure_items
            if getattr(failure, "submit_state_unknown", False) and getattr(failure, "message", "")
        ]
        partial_detail = (
            {
                "requested_n": n,
                "saved_n": saved_count,
                "skipped_n": skipped_n,
                "errors": partial_errors[:5],
                **({"actual_sizes": actual_sizes[:5]} if actual_sizes else {}),
                **({"submit_state_unknown": True} if has_unknown_gateway_failure else {}),
            }
            if saved_count < n or image_errors
            else None
        )
        if has_unknown_gateway_failure:
            hold_image_submit_unknown_for_reconciliation(
                db,
                task_id,
                "; ".join(unknown_errors or partial_errors or ["unknown image submit state"])[:500],
                written_keys=written_keys,
                saved_count=saved_count,
                requested_count=n,
                params_update=pixel_lock_params_update,
            )
            return
        try:
            terminal_task = acquire_image_terminal_boundary(db, task_id)
            if not terminal_task:
                db.rollback()
                unlink_keys(written_keys)
                return
            raise_if_cancel_requested(db, terminal_task)
            final_params = dict(terminal_task.params or {})
            final_params.update(pixel_lock_params_update)
            if partial_detail:
                final_params.update(
                    {
                        "_partial": True,
                        "_requested_n": n,
                        "_saved_n": saved_count,
                        "_skipped_n": skipped_n,
                        "_partial_errors": partial_errors[:5],
                        "_image_result_keys": list(written_keys),
                        **({"_image_submit_state_unknown": True} if has_unknown_gateway_failure else {}),
                        **({"_unknown_submit_errors": unknown_errors[:5]} if unknown_errors else {}),
                    }
                )
            elif actual_sizes:
                final_params["_actual_sizes"] = actual_sizes[:20]
            terminal_task.params = final_params
            db.add_all(pending_assets)

            if not claim_terminal(db, task_id, "succeeded", cost_settled=real_cost):
                db.rollback()
                unlink_keys(written_keys)
                return
            credits.settle(
                db,
                task.user_id,
                reserved=task.cost_frozen,
                real_cost=real_cost,
                biz_ref=task.id,
                commit=False,
            )
            db.commit()
        except TaskCanceled:
            db.rollback()
            unlink_keys(written_keys)
            raise
        except Exception as e:  # noqa: BLE001
            hold_image_success_for_reconciliation(
                db,
                task_id,
                f"图片本地落账失败:{e}",
                written_keys=written_keys,
                saved_count=saved_count,
                params_update=pixel_lock_params_update,
            )
            raise
        if partial_detail:
            usage.record_call(
                db,
                kind="image",
                model_id=model.model_id,
                user_id=task.user_id,
                task_id=task.id,
                status="ok",
                detail=partial_detail,
            )
        set_progress(task_id, 100, "succeeded")
        publish_task_update(task, "succeeded")
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
