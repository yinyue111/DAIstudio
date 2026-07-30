"""Video submit and polling orchestration."""

from __future__ import annotations

import logging
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from billiard.exceptions import SoftTimeLimitExceeded  # noqa: F401 - legacy facade export
from sqlalchemy import or_, update

from ..db import SessionLocal
from ..models import GenAsset, GenTask, ReverseResultRevision, UploadedAsset
from . import credits, gateway, locks, storage, usage
from .config_store import get_model_config
from .generation_common import (
    TaskCanceled,
    TaskLockedError,
    cancel_and_refund,
    fail_and_refund,
    mark_needs_review,
    publish_task_update,
    raise_if_cancel_requested,
)
from .generation_media import (
    final_prompt,
    gateway_reference_image,
    gateway_video_first_frame,
    reference_dimensions,
    video_preview_resolution,
    video_ratio,
    video_render_duration,
    video_target_duration,
    video_target_resolution,
)
from .generation_model_runtime import (  # noqa: F401 - polling facade dependencies
    ModelSnapshotMismatchError,
    find_video_by_request_id_with_model_config,
    model_config_for_task,
    model_from_snapshot,
    poll_video_with_model_config,
    submit_video_with_model_config,
)
from .generation_prompts import (
    product_video_negative_prompt,
)
from .generation_state import TERMINAL_STATUSES as TERMINAL
from .generation_state import cancel_requested, claim_terminal
from .generation_video_download import hold_video_download_for_reconciliation
from .generation_video_flow import (  # noqa: F401 - polling facade dependencies
    POLL_MAX_CONSEC_ERRORS,
    VIDEO_POLL_MAX_SECONDS,
    aware,
    bump_poll_errors,
    enqueue_poll,
    enqueue_video_download,
    mark_poll_alive,
    persist_video_download_result,
    reset_poll_errors,
    video_task_action,
)
from .model_pricing import usage_from_response  # noqa: F401 - polling facade dependency
from .progress import set_progress
from .video_prompt_compiler import (
    COMPILER_VERSION,
    SEEDANCE_EXECUTION_REVISION,
    VIDEO_SUBMIT_CONTRACT_VERSION,
    build_video_prompt_references,
    compile_video_prompt,
    infer_video_model_profile,
    is_seedance_15_profile,
    source_video_is_analysis_only,
    store_video_prompt_compile,
)

log = logging.getLogger("generation")
VIDEO_FIRST_FRAME_MIN_SIDE = 300
VIDEO_FIRST_FRAME_MAX_SIDE = 768
PRODUCT_VIDEO_REFERENCE_MAX_SIDE = 2048
# 源视频参考地址的有效期：上游在提交后短时间内拉取源视频，1 小时留足余量。
VIDEO_SOURCE_URL_TTL_SECONDS = 3600


class VideoSubmitVersionMismatch(RuntimeError):
    """The API-created task cannot be safely submitted by this Worker build."""


class SourceVideoUrlUnavailable(RuntimeError):
    """本部署无法为源视频生成上游可访问地址（例如本地存储 + 上传原片）。

    与其他失败不同，这一类是"部署能力不足"而非"请求非法"：调用方可以
    退回首帧生成，但必须显式标注降级，绝不允许静默丢失运动信息。
    """


def public_video_submit_error(exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if "sensitive information" in lowered or "内容安全" in message or "敏感" in message:
        return (
            "提示词触发了视频模型的内容安全检查，已退回冻结积分。"
            "请删除可能被误判的重复品牌词、字幕或过度动作描述后重试。"
        )
    if isinstance(exc, gateway.GatewayError) and getattr(exc, "status_code", None) == 400:
        return (
            "视频模型拒绝了当前提示词或生成参数，已退回冻结积分。"
            "请缩短提示词，或检查当前模型支持的时长、比例和参考图方式。"
        )
    if "timed out" in lowered or "timeout" in lowered or "超时" in message:
        return "视频提交等待超时，已退回冻结积分，请稍后重试。"
    if "没有可用账号" in message or "no available compatible accounts" in lowered:
        return "视频网关当前没有可用账号支持该模型，已退回冻结积分。"
    return "视频提交失败，已退回冻结积分，请稍后重试"


def lineage_video_analysis_for_compile(payload: Any) -> dict[str, Any] | None:
    """Return the reverse-lineage video_analysis if it carries evidence-gated shots.

    仅当分镜带有证据门判定（shot["evidence_gate"]）时才返回——没有证据门
    的旧分析对编译器没有增量信息，返回 None 让编译走与旧版完全一致的路径。
    """
    if not isinstance(payload, dict):
        return None
    analysis = payload.get("video_analysis")
    if not isinstance(analysis, dict):
        return None
    shots = analysis.get("shots")
    if not isinstance(shots, list):
        return None
    if any(isinstance(row, dict) and isinstance(row.get("evidence_gate"), dict) for row in shots):
        return analysis
    return None


def merge_lineage_video_analysis(
    raw_prompt: Any,
    analysis: dict[str, Any] | None,
) -> Any:
    """Attach evidence-gated reverse analysis to the compiler input.

    向后兼容约束：
    - 字符串提示词（旧任务/direct 输入）原样返回；
    - 提示词已自带 video_analysis 时不覆盖（客户端显式传入优先）；
    - 没有可用证据分析时原样返回；
    - 永不修改传入的 prompt dict（返回浅拷贝），存库的 prompt 与请求指纹不变。
    """
    if not isinstance(analysis, dict) or not isinstance(raw_prompt, dict):
        return raw_prompt
    if isinstance(raw_prompt.get("video_analysis"), dict):
        return raw_prompt
    return {**raw_prompt, "video_analysis": deepcopy(analysis)}


def _task_lineage_video_analysis(db, task) -> dict[str, Any] | None:
    """Load the applied reverse revision bound to a task and extract gated analysis."""
    operation_id = getattr(task, "reverse_operation_id", None)
    revision_id = getattr(task, "source_revision_id", None)
    if db is None or operation_id is None or revision_id is None:
        return None
    revision = db.get(ReverseResultRevision, int(revision_id))
    if (
        revision is None
        or int(revision.operation_id) != int(operation_id)
        or int(revision.user_id) != int(getattr(task, "user_id", 0) or 0)
    ):
        return None
    return lineage_video_analysis_for_compile(
        revision.payload if isinstance(revision.payload, dict) else None
    )


def _compile_legacy_video_prompt(task: GenTask, model, params: dict, db=None) -> tuple[str, dict]:
    """Compile retry/legacy tasks that predate request-time video compilation."""
    references = build_video_prompt_references(
        source_asset_url=task.source_asset_url,
        source_type=task.source_type,
        params=params,
        source_video_analysis_only=source_video_is_analysis_only(params),
    )
    extra = getattr(model, "extra", None) if isinstance(getattr(model, "extra", None), dict) else {}
    model_profiles = extra.get("video_prompt_profiles") or extra.get("prompt_profiles")
    if not isinstance(model_profiles, dict):
        model_profiles = None
    compiled = compile_video_prompt(
        merge_lineage_video_analysis(
            task.prompt or final_prompt(task),
            _task_lineage_video_analysis(db, task),
        ),
        duration=video_render_duration(params, task.stage),
        model_id=str(getattr(model, "model_id", "") or ""),
        provider=str(getattr(model, "provider", "") or ""),
        extra=extra,
        references=references,
        product_reference=any(item.get("role") == "product" for item in references),
        portrait_reference=any(item.get("role") == "character" for item in references),
        product_lock_mode=str(params.get("product_lock_mode") or "locked"),
        product_video_template=str(params.get("product_video_template") or "prompt_driven"),
        model_profiles=model_profiles,
        fit_mode="single_clip",
    )
    if compiled.get("sequence_required"):
        raise RuntimeError("核心单视频提示词自动精简后仍超限，请减少动作描述或技术约束后重试")

    persisted = dict(params)
    store_video_prompt_compile(persisted, compiled, references)
    return persisted["_generation_prompt"], persisted


def _enqueue_video_download_safely(
    db,
    task_id: int,
    *,
    countdown: int = 0,
    external_task_id: str | None = None,
) -> None:
    try:
        enqueue_video_download(
            task_id,
            countdown=countdown,
            external_task_id=external_task_id,
        )
    except Exception as e:  # noqa: BLE001
        hold_video_download_for_reconciliation(
            db,
            task_id,
            str(e),
            expected_status="running",
            expected_phase="downloading",
            expected_external_task_id=external_task_id,
        )


def hold_video_submit_unknown_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    request_id: str | None = None,
    expected_params: dict | None = None,
    params_update: dict | None = None,
) -> None:
    """Hold video submits that may already be accepted upstream."""
    db.rollback()
    task = db.get(GenTask, task_id, populate_existing=True)
    observed_params = task.params if task and isinstance(task.params, dict) else {}
    expected_request_id = request_id or observed_params.get("_video_request_id")
    expected_snapshot = expected_params if expected_params is not None else observed_params
    if (
        not task
        or task.status != "running"
        or task.phase != "submitting"
        or task.external_task_id is not None
        or observed_params != expected_snapshot
        or not expected_request_id
        or str(observed_params.get("_video_request_id") or "") != str(expected_request_id)
    ):
        return
    params = dict(observed_params)
    if params_update:
        params.update(params_update)
    params["_video_submit_state_unknown"] = True
    mark_needs_review(
        db,
        task_id,
        (
            "视频提交状态未知,上游可能已接受任务,冻结积分暂不退回。"
            "请通过外部任务结果补结果结算,或确认未生成后人工退款。"
            f"request_id={params.get('_video_request_id') or params.get('request_id') or 'unknown'}; "
            f"error={error[:500]}"
        ),
        params_update=params,
        rollback=False,
        expected_status="running",
        expected_phase="submitting",
        require_no_external_task_id=True,
    )


def gateway_source_video_url(db, task: GenTask) -> str:
    """Resolve the task's source video into a URL the video gateway can fetch.

    真·视频参考通道的入口。请求非法（缺素材、外部链接、未解锁等）一律显式
    报错并中止提交。唯一例外是"本部署拿不到上游可访问地址"——那会抛
    ``SourceVideoUrlUnavailable``，调用方可退回首帧，但必须标注降级，
    绝不允许静默丢失运动信息。
    """
    raw = str(task.source_asset_url or "").strip()
    if not raw:
        raise RuntimeError("视频参考生成缺少源视频素材，无法提交")
    key = storage.key_from_url(raw)
    if not key:
        raise RuntimeError("视频参考生成的源视频必须是本站素材，无法提交外部视频链接")
    if key.startswith("upload_video/"):
        row = db.get(UploadedAsset, key)
        if not row or row.user_id != task.user_id:
            raise RuntimeError("上传视频不存在")
    elif key.startswith(("video_preview/", "video_hd/")):
        # 与 asset_refs.generated_video_reference_path 相同的归属校验，
        # 但不落盘取文件——这里只需要一个上游可访问的地址。
        asset = (
            db.query(GenAsset)
            .filter(
                or_(
                    GenAsset.preview_url == storage.public_url(key),
                    GenAsset.hd_url == storage.public_url(key),
                )
            )
            .first()
        )
        if not asset or asset.user_id != task.user_id:
            raise RuntimeError("生成视频不存在")
        if key.startswith("video_hd/") and not asset.unlocked:
            raise RuntimeError("请先解锁该视频后再作为参考")
    else:
        raise RuntimeError("视频参考生成的源视频类型不受支持")
    if storage.is_object_storage_enabled():
        url = storage.presigned_download_url(key, expires=VIDEO_SOURCE_URL_TTL_SECONDS)
    elif key.startswith("video_preview/"):
        # 本地存储只公开预览目录；上传原片与高清片没有上游可访问地址。
        url = storage.public_url(key)
    else:
        raise SourceVideoUrlUnavailable("当前部署未启用对象存储，无法为上传原片生成上游可访问地址")
    if not url:
        raise SourceVideoUrlUnavailable("无法为源视频生成上游可访问地址")
    return url


def video_submit_params(db, task: GenTask) -> dict:
    """Stage-aware effective gateway params for a video render."""
    params = dict(task.params or {})
    target_resolution = video_target_resolution(params)
    target_duration = video_target_duration(params)
    # preview = cheap/short low-res render; final = selected quality
    if task.stage == "preview":
        params["target_resolution"] = target_resolution
        params["target_duration"] = target_duration
        params["resolution"] = video_preview_resolution(target_resolution)
        params["duration"] = video_render_duration(params, task.stage)
    else:
        params["target_resolution"] = target_resolution
        params["target_duration"] = target_duration
        params["resolution"] = target_resolution
        params["duration"] = target_duration
    ref_w, ref_h = reference_dimensions(task)
    if not params.get("ratio"):
        params["ratio"] = video_ratio(ref_w, ref_h)
    subject_mode = str(params.get("subject_mode") or "").lower()
    is_product_image_video = task.source_type == "image" and subject_mode == "product"
    if is_product_image_video:
        params["negative_prompt"] = product_video_negative_prompt(params.get("negative_prompt"))
        product_reference = (
            params.get("product_reference_image")
            or params.get("reference_image_url")
            or task.source_asset_url
        )
        if product_reference:
            params["product_reference_image"] = gateway_reference_image(
                db,
                task,
                product_reference,
                min_side=VIDEO_FIRST_FRAME_MIN_SIDE,
                max_side=PRODUCT_VIDEO_REFERENCE_MAX_SIDE,
                prefer_original_upload=True,
                quality=92,
                subsampling=0,
            )
        if not params.get("first_frame_image"):
            params.pop("reference_image_url", None)
    product_details = params.get("product_detail_images")
    if isinstance(product_details, list) and product_details:
        params["product_detail_images"] = [
            gateway_reference_image(
                db,
                task,
                detail_url,
                min_side=VIDEO_FIRST_FRAME_MIN_SIDE,
                max_side=PRODUCT_VIDEO_REFERENCE_MAX_SIDE,
                prefer_original_upload=True,
                quality=92,
                subsampling=0,
            )
            for detail_url in product_details
        ]

    # 真·视频参考通道：非"仅反推分析"的视频源任务，把源视频 URL 显式带进
    # 提交参数（payload 构建方会把它写入上游允许的视频字段），而不是只抽首帧。
    if (
        task.source_type == "video"
        and task.source_asset_url
        and not source_video_is_analysis_only(params)
    ):
        try:
            params["source_video_url"] = gateway_source_video_url(db, task)
            params.pop("source_video_degraded", None)
            params.pop("source_video_degraded_reason", None)
        except SourceVideoUrlUnavailable as exc:
            # 部署能力不足（本地存储 + 上传原片）：退回首帧，但显式标注降级。
            # 这里刻意不抛错——否则本地存储部署的视频参考生成会整体不可用；
            # 也刻意不静默——运动信息丢失必须让用户看得到。
            params["source_video_degraded"] = "first_frame_only"
            params["source_video_degraded_reason"] = (
                f"{exc}。本次仅使用首帧，未传递运动信息；" "如需完整视频参考，请启用对象存储。"
            )
            params.pop("source_video_url", None)
            log.warning(
                "video source degraded to first frame: task=%s reason=%s",
                task.id,
                exc,
            )

    # Every client-supplied frame URL must be resolved by this backend before
    # it reaches the model gateway. Product identity references stay separate
    # so the provider does not interpret them as the opening or closing frame.
    first_frame = params.get("first_frame_image")
    if not is_product_image_video:
        first_frame = first_frame or params.get("reference_image_url")
        if task.source_type == "image":
            first_frame = first_frame or task.source_asset_url
        elif (
            task.source_type == "video"
            and not first_frame
            and not source_video_is_analysis_only(params)
        ):
            first_frame = gateway_video_first_frame(db, task)
    frame_reference_kwargs = {
        "min_side": VIDEO_FIRST_FRAME_MIN_SIDE,
        "max_side": VIDEO_FIRST_FRAME_MAX_SIDE,
    }
    safe_first_frame = ""
    if first_frame:
        safe_first_frame = gateway_reference_image(
            db,
            task,
            first_frame,
            **frame_reference_kwargs,
        )
        params["first_frame_image"] = safe_first_frame
        if params.get("reference_image_url"):
            params["reference_image_url"] = safe_first_frame
    last_frame = params.get("last_frame_image")
    if last_frame:
        params["last_frame_image"] = gateway_reference_image(
            db,
            task,
            last_frame,
            **frame_reference_kwargs,
        )
    elif safe_first_frame and task.source_type == "image" and subject_mode == "portrait":
        params["last_frame_image"] = safe_first_frame
        params["_portrait_locked"] = True
    style_ref = params.get("style_reference_image")
    if style_ref:
        params["style_reference_image"] = gateway_reference_image(
            db,
            task,
            style_ref,
            min_side=VIDEO_FIRST_FRAME_MIN_SIDE,
            max_side=VIDEO_FIRST_FRAME_MAX_SIDE,
        )
    character_ref = params.get("character_reference_image")
    if character_ref:
        safe_character_ref = gateway_reference_image(
            db,
            task,
            character_ref,
            min_side=VIDEO_FIRST_FRAME_MIN_SIDE,
            max_side=VIDEO_FIRST_FRAME_MAX_SIDE,
        )
        params["character_reference_image"] = safe_character_ref
        if str(params.get("subject_mode") or "").lower() == "portrait":
            params.setdefault("first_frame_image", safe_character_ref)
            params.setdefault("reference_image_url", safe_character_ref)
            params.setdefault("last_frame_image", safe_character_ref)
            params["_portrait_locked"] = True
    return params


def video_persisted_params(task: GenTask, original: dict, submitted: dict) -> dict:
    """Persist user intent, not just the low-cost preview submit envelope."""
    persisted = dict(original)
    target_resolution = submitted.get("target_resolution")
    target_duration = submitted.get("target_duration")
    if target_resolution:
        persisted["target_resolution"] = target_resolution
    if target_duration:
        persisted["target_duration"] = target_duration
    if task.stage == "preview":
        if target_resolution:
            persisted["resolution"] = target_resolution
        if target_duration:
            persisted["duration"] = target_duration
    else:
        if submitted.get("resolution"):
            persisted["resolution"] = submitted["resolution"]
        if submitted.get("duration"):
            persisted["duration"] = submitted["duration"]
    for key in ("ratio",):
        if submitted.get(key):
            persisted[key] = submitted[key]
    subject_mode = str(original.get("subject_mode") or "").lower()
    if original.get("product_reference_image"):
        persisted["product_reference_image"] = original["product_reference_image"]
    elif subject_mode == "product" and task.source_type == "image" and task.source_asset_url:
        persisted["product_reference_image"] = task.source_asset_url
    if "product_detail_images" in original:
        persisted["product_detail_images"] = list(original.get("product_detail_images") or [])
    if original.get("first_frame_image"):
        persisted["first_frame_image"] = original["first_frame_image"]
    elif subject_mode != "product" and task.source_type == "image" and task.source_asset_url:
        persisted["first_frame_image"] = task.source_asset_url
    if original.get("last_frame_image"):
        persisted["last_frame_image"] = original["last_frame_image"]
    if original.get("character_reference_image"):
        persisted["character_reference_image"] = original["character_reference_image"]
    for key in ("subject_mode",):
        if original.get(key):
            persisted[key] = original[key]
    if task.stage != "preview":
        return persisted
    persisted["preview_resolution"] = submitted.get("resolution")
    persisted["preview_duration"] = submitted.get("duration")
    return persisted


def persist_video_submit_result(
    db,
    task_id: int,
    *,
    request_id: str,
    external_task_id: str,
    submitted_params: dict,
    provider_status: str | None = None,
    result_url: str | None = None,
) -> str | None:
    """Fence a provider submit result against the latest task generation.

    Provider submits are non-idempotent and may run while another transaction
    requests cancellation. Always merge into freshly observed params so a stale
    submit worker cannot erase that request and continue into polling/settlement.
    """
    target_phase = "downloading" if provider_status == "succeeded" and result_url else "polling"
    for _attempt in range(3):
        db.rollback()
        task = db.get(GenTask, task_id, populate_existing=True)
        if not task:
            return None
        observed_params = task.params if isinstance(task.params, dict) else {}
        if (
            task.status != "running"
            or task.phase != "submitting"
            or task.external_task_id is not None
            or str(observed_params.get("_video_request_id") or "") != str(request_id)
        ):
            return None

        persisted = video_persisted_params(task, dict(observed_params), submitted_params)
        if result_url:
            persisted["_video_result_url"] = result_url
        canceled = cancel_requested(task)
        durable_phase = "reconciling" if canceled else target_phase
        updated = db.execute(
            update(GenTask)
            .where(
                GenTask.id == task_id,
                GenTask.status == "running",
                GenTask.phase == "submitting",
                GenTask.external_task_id.is_(None),
                GenTask.params == task.params,
            )
            .values(
                params=persisted,
                external_task_id=str(external_task_id),
                external_submitted_at=datetime.now(timezone.utc),
                phase=durable_phase,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if (updated or 0) != 1:
            continue

        db.commit()

        if canceled:
            try:
                db.rollback()
                current = db.get(GenTask, task_id, populate_existing=True)
                if (
                    not current
                    or not cancel_requested(current)
                    or not claim_terminal(
                        db,
                        task_id,
                        "canceled",
                        error="用户已取消任务",
                        expected_status="running",
                        expected_phase=durable_phase,
                        expected_external_task_id=str(external_task_id),
                    )
                ):
                    db.rollback()
                    return None
                if current.cost_frozen and int(current.cost_settled or 0) == 0:
                    credits.refund(
                        db,
                        current.user_id,
                        current.cost_frozen,
                        biz_ref=current.id,
                        commit=False,
                    )
                db.commit()
                set_progress(task_id, 100, "canceled")
                publish_task_update(current, "canceled")
                return "canceled"
            except Exception as e:  # noqa: BLE001
                log.exception("video submit cancel refund failed for task %s", task_id)
                db.rollback()
                mark_needs_review(
                    db,
                    task_id,
                    f"视频已由上游接受,但取消退款失败,需要人工对账: {e}",
                    expected_status="running",
                    expected_phase=durable_phase,
                    expected_external_task_id=str(external_task_id),
                )
                return "needs_review"

        set_progress(task_id, 60 if target_phase == "downloading" else 30, "running")
        return target_phase
    db.rollback()
    return None


def recover_unknown_submit_by_request_id(
    db,
    task: GenTask,
    model,
    original_params: dict,
    submitted_params: dict,
) -> bool:
    request_id = original_params.get("_video_request_id") or submitted_params.get("request_id")
    if not request_id:
        return False
    try:
        found = find_video_by_request_id_with_model_config(model, str(request_id))
    except Exception as e:  # noqa: BLE001
        usage.record_call(
            db,
            kind="video_submit",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="failed",
            detail={"reconcile": True, "request_id": request_id, "error": str(e)[:300]},
        )
        return False
    if not found:
        usage.record_call(
            db,
            kind="video_submit",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="failed",
            detail={"reconcile": True, "request_id": request_id, "result": "miss"},
        )
        return False

    result_url = found.get("url")
    phase = persist_video_submit_result(
        db,
        task.id,
        request_id=str(request_id),
        external_task_id=str(found["external_task_id"]),
        submitted_params=submitted_params,
        provider_status=found.get("status"),
        result_url=result_url,
    )
    if phase not in ("polling", "downloading"):
        return False
    task = db.get(GenTask, task.id, populate_existing=True)
    usage.record_call(
        db,
        kind="video_submit",
        model_id=model.model_id,
        user_id=task.user_id,
        task_id=task.id,
        status="ok",
        detail={
            "reconcile": True,
            "request_id": request_id,
            "external_task_id": task.external_task_id,
            "provider_status": found.get("status"),
        },
    )
    return True


def submit_state_unknown(exc: Exception) -> bool:
    """True when the upstream submit may have been accepted."""
    if isinstance(exc, gateway.GatewayError):
        explicit = getattr(exc, "submit_state_unknown", None)
        if explicit is not None:
            return bool(explicit)
        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            return status_code == 429 or 500 <= int(status_code) < 600
        return bool(getattr(exc, "transient", False))
    return True


def _prepare_video_submission(db, task: GenTask, model) -> tuple[dict, str, dict]:
    original_params = dict(task.params or {})
    prompt = str(original_params.get("_generation_prompt") or "").strip()
    stored_compiler_version = str(original_params.get("_prompt_compiler_version") or "").strip()
    stored_contract_version = str(
        original_params.get("_video_submit_contract_version") or ""
    ).strip()
    model_extra = (
        getattr(model, "extra", None)
        if isinstance(getattr(model, "extra", None), dict)
        else {}
    )
    model_profiles = model_extra.get("video_prompt_profiles") or model_extra.get(
        "prompt_profiles"
    )
    if not isinstance(model_profiles, dict):
        model_profiles = None
    model_profile = infer_video_model_profile(
        model_id=str(getattr(model, "model_id", "") or ""),
        provider=str(getattr(model, "provider", "") or ""),
        duration=video_render_duration(original_params, task.stage),
        extra=model_extra,
        model_profiles=model_profiles,
    )
    needs_seedance_safety_recompile = (
        is_seedance_15_profile(
            family=str(model_profile.get("family") or ""),
            model_id=str(getattr(model, "model_id", "") or ""),
        )
        and str(original_params.get("_seedance_execution_revision") or "").strip()
        != SEEDANCE_EXECUTION_REVISION
    )
    if not prompt or not stored_compiler_version or not stored_contract_version:
        prompt, original_params = _compile_legacy_video_prompt(task, model, original_params, db=db)
    elif stored_contract_version != VIDEO_SUBMIT_CONTRACT_VERSION:
        raise VideoSubmitVersionMismatch(
            "视频提交契约版本不一致，已停止提交；请重启 API 与 Worker 后重试"
        )
    elif stored_compiler_version != COMPILER_VERSION:
        raise VideoSubmitVersionMismatch(
            "视频提示词编译器版本不一致，已停止提交；请重启 API 与 Worker 后重试"
        )
    elif needs_seedance_safety_recompile:
        prompt, original_params = _compile_legacy_video_prompt(task, model, original_params, db=db)
    if not prompt:
        raise RuntimeError("视频提示词为空，无法提交生成")
    if not original_params.get("_video_request_id"):
        original_params["_video_request_id"] = f"video-{task.id}-{uuid4().hex}"
    task.params = original_params
    db.commit()
    params = video_submit_params(db, task)
    params.setdefault("request_id", original_params["_video_request_id"])
    raise_if_cancel_requested(db, db.get(GenTask, task.id))
    return original_params, prompt, params


def start_video_task(
    task_id: int,
    *,
    get_model_config_fn=None,
    submit_video_fn=None,
    try_enqueue_poll_fn=None,
    try_enqueue_video_download_fn=None,
) -> None:
    """Submit the async video render, then hand off to the non-blocking poller."""
    model_loader = get_model_config_fn or get_model_config
    submitter = submit_video_fn or submit_video_with_model_config
    enqueue_poll_fn = try_enqueue_poll_fn or _enqueue_poll_safely
    enqueue_download_fn = try_enqueue_video_download_fn or _enqueue_video_download_safely
    submitted_external_task_id: str | None = None
    lock_key = f"gen:lock:{task_id}"
    lock_token = locks.acquire(lock_key)
    if not lock_token:
        log.warning("video task %s locked by another worker, retry later", task_id)
        raise TaskLockedError(f"video task {task_id} locked")
    submitted = False
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in TERMINAL:
            return
        raise_if_cancel_requested(db, task)
        if task.external_task_id:
            action = video_task_action(task)
            if action == "download":
                _handoff_video_download_best_effort(
                    db,
                    task_id,
                    task.external_task_id,
                    try_enqueue_video_download_fn,
                )
                return
            if action != "poll":
                return
            submitted = True
            submitted_external_task_id = task.external_task_id
        else:
            claimed = db.execute(
                update(GenTask)
                .where(GenTask.id == task_id, GenTask.status == "queued")
                .values(status="running", phase="submitting")
            ).rowcount
            if (claimed or 0) != 1:
                return
            db.commit()
            set_progress(task_id, 10, "running")
            task = db.get(GenTask, task_id)
            raise_if_cancel_requested(db, task)

            model = model_config_for_task(db, task, "video", model_loader)
            if not model or not model.enabled:
                raise RuntimeError("未配置可用的视频模型")
            model = model_from_snapshot(task, model, db)

            original_params, prompt, params = _prepare_video_submission(db, task, model)

            submit_t0 = time.time()
            try:
                ext_id = submitter(model, prompt, params)
            except Exception as e:  # noqa: BLE001
                unknown = submit_state_unknown(e)
                usage.record_call(
                    db,
                    kind="video_submit",
                    model_id=model.model_id,
                    user_id=task.user_id,
                    task_id=task.id,
                    status="failed",
                    latency_ms=int((time.time() - submit_t0) * 1000),
                    detail={
                        "stage": task.stage,
                        "resolution": params.get("resolution"),
                        "target_resolution": params.get("target_resolution"),
                        "duration": params.get("duration"),
                        "target_duration": params.get("target_duration"),
                        "ratio": params.get("ratio"),
                        "request_id": params.get("request_id"),
                        "submit_state_unknown": unknown,
                        "error": str(e)[:300],
                    },
                )
                if unknown:
                    recovered = recover_unknown_submit_by_request_id(
                        db, task, model, original_params, params
                    )
                    if recovered:
                        action = video_task_action(task)
                        if action == "download":
                            enqueue_download_fn(
                                db,
                                task_id,
                                external_task_id=task.external_task_id,
                            )
                        elif action == "poll":
                            mark_poll_alive(task_id, task.external_task_id)
                            enqueue_poll_fn(task_id, task.external_task_id)
                        return
                    hold_video_submit_unknown_for_reconciliation(
                        db,
                        task_id,
                        str(e),
                        request_id=str(original_params["_video_request_id"]),
                        expected_params=original_params,
                        params_update=video_persisted_params(task, original_params, params),
                    )
                    return
                raise
            phase = persist_video_submit_result(
                db,
                task_id,
                request_id=str(original_params["_video_request_id"]),
                external_task_id=str(ext_id),
                submitted_params=params,
            )
            if phase != "polling":
                return
            submitted_external_task_id = str(ext_id)
            # The external task id is the recovery point. Persist it before any
            # best-effort accounting/progress side effects so a worker crash
            # after upstream acceptance can be resumed instead of stranded in
            # submitting.
            try:
                usage.record_call(
                    db,
                    kind="video_submit",
                    model_id=model.model_id,
                    user_id=task.user_id,
                    task_id=task.id,
                    status="ok",
                    latency_ms=int((time.time() - submit_t0) * 1000),
                    detail={
                        "stage": task.stage,
                        "external_task_id": ext_id,
                        "resolution": params.get("resolution"),
                        "target_resolution": params.get("target_resolution"),
                        "duration": params.get("duration"),
                        "target_duration": params.get("target_duration"),
                        "ratio": params.get("ratio"),
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("video submit usage record failed for task %s", task_id)
            set_progress(task_id, 30, "running")
            submitted = True
    except TaskCanceled as e:
        cancel_and_refund(db, task_id, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("video submit %s failed", task_id)
        current = db.get(GenTask, task_id)
        if submitted or (current and current.external_task_id):
            action = video_task_action(current)
            if action == "download":
                _handoff_video_download_best_effort(
                    db,
                    task_id,
                    current.external_task_id,
                    try_enqueue_video_download_fn,
                )
            elif action == "poll":
                mark_poll_alive(task_id, current.external_task_id)
                enqueue_poll_fn(task_id, current.external_task_id)
            return
        public_error = (
            str(e)
            if isinstance(e, VideoSubmitVersionMismatch)
            else public_video_submit_error(e)
        )
        fail_and_refund(db, task_id, str(e), public_error=public_error)
    finally:
        db.close()
        locks.release(lock_key, lock_token)

    if submitted:
        mark_poll_alive(task_id, submitted_external_task_id)
        enqueue_poll_fn(task_id, submitted_external_task_id)


# Polling remains import-compatible through this service module.
from . import generation_video_polling as _generation_video_polling_module  # noqa: E402
from .compat_facade import (  # noqa: E402
    install_assignment_forwarding as _install_assignment_forwarding,
)
from .generation_video_polling import (  # noqa: E402, F401
    _continue_pending_poll,
    _enqueue_owned_video_download,
    _enqueue_poll_safely,
    _expire_video_poll,
    _handle_failed_poll_result,
    _handle_poll_provider_error,
    _handle_succeeded_poll_result,
    _handoff_video_download_best_effort,
    _hold_owned_video_download_for_reconciliation,
    _load_pollable_video_task,
    _poll_owner_state,
    _poll_runtime_model,
    _poll_timing,
    _reconcile_poll_exception,
    _reconcile_poll_soft_timeout,
    _reload_polled_video_task,
    hold_video_poll_for_reconciliation,
    poll_video_once,
)

_install_assignment_forwarding(__name__, (_generation_video_polling_module,))
