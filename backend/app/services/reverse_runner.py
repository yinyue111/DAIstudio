"""Reverse-operation execution engine and stale-run reaper."""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import (
    ReverseOperation,
    User,
)
from ..schemas import ReverseIn, ReverseOperationCreate
from . import (
    asset_refs,
    credits,  # noqa: F401 - forwards legacy reverse_operations monkeypatches
    gateway,
    image_evidence_analysis,
    reverse_lineage,
    reverse_source_resolution,
    storage,
    usage,
    video_audio,
    video_evidence_analysis,
)
from . import (
    reverse_reaper as _reverse_reaper_module,
)
from . import reverse_result_processing as _reverse_result_processing_module
from . import reverse_settlement as _reverse_settlement_module
from .compat_facade import install_assignment_forwarding as _install_assignment_forwarding
from .config_store import (
    ModelConfigResolutionError,
    resolve_model_config,
)
from .model_gateway_config import runtime_config_for_model
from .reverse_quotes import (
    CONFIRMATION_TTL,
    IMAGE_EVIDENCE_TARGETS,
    ReverseOperationInvalid,
    _log_operation_event,
    _source_ranges_payload,
    utcnow,
)
from .reverse_result_processing import (  # noqa: F401
    _OCR_CLAIM_QUOTE_PAIRS,
    _apply_image_ocr_gate_to_text,
    _finalize_evidence_bound_video_result,
    _has_timestamped_audio_evidence,
    _merge_video_analysis_result,
    _replace_ocr_claim,
    _tidy_gated_text_field,
    validate_reverse_result,
)
from .reverse_settlement import (  # noqa: F401
    _history_title,
    _record_gateway_failure,
    _remember_history,
)
from .video_analysis import (
    max_frame_count,
    normalize_video_analysis_preset,
)

log = logging.getLogger("reverse_operations")


def _claim_operation(db: Session, operation_id: int) -> ReverseOperation | None:
    now = utcnow()
    claimed = db.execute(
        update(ReverseOperation)
        .where(
            ReverseOperation.id == operation_id,
            ReverseOperation.status == "queued",
        )
        .values(
            status="running",
            phase="resolving_asset",
            progress=5,
            attempt_count=func.coalesce(ReverseOperation.attempt_count, 0) + 1,
            started_at=func.coalesce(ReverseOperation.started_at, now),
            updated_at=now,
        )
        .returning(ReverseOperation.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if claimed is None:
        db.rollback()
        return None
    db.commit()
    operation = db.get(ReverseOperation, operation_id)
    if operation is not None:
        _log_operation_event(
            "reverse_operation_claimed",
            operation_id=operation.id,
            status=operation.status,
            phase=operation.phase,
            error_code=operation.error_code,
            attempt_count=int(operation.attempt_count or 0),
        )
    return operation


def _set_phase(db: Session, operation_id: int, phase: str, progress: int) -> bool:
    from . import reverse_operations as _ro

    operation = db.execute(
        select(ReverseOperation)
        .where(ReverseOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None or operation.status != "running":
        db.rollback()
        return False
    if operation.cancel_requested:
        _ro._refund_locked(db, operation, note="cooperative reverse cancellation")
        operation.status = "canceled"
        operation.phase = None
        operation.progress = 100
        operation.error_code = "CANCELED"
        operation.finished_at = utcnow()
        operation.updated_at = utcnow()
        db.commit()
        _log_operation_event(
            "reverse_operation_canceled",
            operation_id=operation.id,
            status=operation.status,
            phase=operation.phase,
            error_code=operation.error_code,
        )
        return False
    operation.phase = phase
    operation.progress = min(max(int(progress), 0), 99)
    operation.updated_at = utcnow()
    db.commit()
    _log_operation_event(
        "reverse_operation_phase_changed",
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
        progress=int(operation.progress or 0),
    )
    return True


def _pause_for_cover(
    db: Session,
    operation_id: int,
    *,
    video_analysis: dict[str, Any] | None,
    reason: str,
) -> None:
    from . import reverse_operations as _ro

    operation = db.execute(
        select(ReverseOperation)
        .where(ReverseOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None or operation.status != "running":
        db.rollback()
        return
    if operation.cancel_requested:
        _ro._refund_locked(db, operation, note="canceled before cover confirmation")
        operation.status = "canceled"
        operation.progress = 100
        operation.phase = None
        operation.error_code = "CANCELED"
        operation.finished_at = utcnow()
    else:
        context = dict(operation.request_context or {})
        context["cover_confirmation_required"] = True
        context["cover_confirmation_required_at"] = utcnow().isoformat()
        operation.request_context = context
        operation.status = "needs_confirmation"
        operation.phase = "awaiting_cover_confirmation"
        operation.progress = 25
        operation.result = {"video_analysis": video_analysis or {"analysis_mode": "unavailable"}}
        operation.error_code = "VIDEO_FRAMES_UNAVAILABLE"
        operation.error = reason[:2000]
        operation.confirmation_expires_at = utcnow() + CONFIRMATION_TTL
    operation.updated_at = utcnow()
    db.commit()
    _log_operation_event(
        (
            "reverse_operation_canceled"
            if operation.status == "canceled"
            else "reverse_operation_needs_confirmation"
        ),
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
    )


def _gateway_accepts(name: str) -> bool:
    try:
        signature = inspect.signature(gateway.reverse_prompt)
    except (TypeError, ValueError):
        return True
    return name in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _fallback_reference_fingerprints(refs: list[str]) -> list[dict[str, Any]]:
    fingerprints: list[dict[str, Any]] = []
    for index, ref in enumerate(refs, start=1):
        if str(ref).startswith("data:"):
            content_hash = reverse_lineage.data_uri_content_hash(ref)
            method = "gateway_reference_sha256"
        else:
            content_hash = reverse_lineage.bytes_content_hash(
                f"mock-url:{ref}".encode()
            )
            method = "mock_locator_sha256"
        fingerprints.append(reverse_lineage.build_source_fingerprint(
            source_index=index,
            content_hash=content_hash,
            locator=ref,
            method=method,
        ))
    return fingerprints


def _audio_analysis_for_request(
    body: ReverseOperationCreate,
    db: Session,
    user: User,
) -> dict[str, Any]:
    source_ranges = _source_ranges_payload(body)
    source_range = source_ranges[0] if len(source_ranges) == 1 else None
    start = float((source_range or {}).get("start_seconds") or 0)
    end = (source_range or {}).get("end_seconds")
    try:
        key = storage.key_from_url(str(body.asset_url or ""))
        if key:
            path = asset_refs.generated_video_reference_path(db, user.id, key)
            if len(source_ranges) > 1:
                return video_audio.analyze_video_audio_ranges_from_path(
                    str(path),
                    source_ranges=source_ranges,
                )
            return video_audio.analyze_video_audio_from_path(
                str(path),
                start_seconds=start,
                end_seconds=end,
            )
        if len(source_ranges) > 1:
            return video_audio.analyze_video_audio_ranges(
                str(body.asset_url or ""),
                source_ranges=source_ranges,
            )
        return video_audio.analyze_video_audio(
            str(body.asset_url or ""),
            start_seconds=start,
            end_seconds=end,
        )
    except Exception as exc:  # noqa: BLE001 - visual analysis remains authoritative
        log.warning("audio evidence analysis degraded: %s", str(exc)[:300])
        return {
            "status": "failed",
            "transcript": "",
            "segments": [],
            "language": None,
            "provider_model": None,
            "degraded_reason": f"音频分析失败：{str(exc)[:160]}",
        }


def _merge_completed_shot_reanalysis(
    db: Session,
    *,
    child_operation,
    child_revision,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    from . import reverse_operations as service

    return _reverse_result_processing_module._merge_completed_shot_reanalysis_impl(
        db,
        child_operation=child_operation,
        child_revision=child_revision,
        result=result,
        revision_shots=service._revision_shots,
        shot_by_id=service._shot_by_id,
        create_result_revision=service.create_result_revision,
    )


def _finish_success(
    db: Session,
    operation_id: int,
    *,
    result: dict[str, Any],
    provider_result: dict[str, Any] | None = None,
    source_fingerprints: list[dict[str, Any]] | None = None,
    reference_count: int,
    real_cost: int,
) -> ReverseOperation | None:
    from . import reverse_operations as service

    return _reverse_settlement_module._finish_success_impl(
        db,
        operation_id,
        result=result,
        provider_result=provider_result,
        source_fingerprints=source_fingerprints,
        reference_count=reference_count,
        real_cost=real_cost,
        refund_locked=service._refund_locked,
        merge_completed_shot_reanalysis=_merge_completed_shot_reanalysis,
    )


@dataclass
class _OperationRunState:
    operation_id: int
    db: Session
    operation: ReverseOperation | None = None
    user: User | None = None
    model: Any = None
    runtime: Any = None
    body: ReverseIn | ReverseOperationCreate | None = None
    context: dict[str, Any] = dataclass_field(default_factory=dict)
    preset: str = "standard"
    gateway_target: str = "image"
    real_cost: int = 0
    visual_cost: int = 0
    audio_surcharge: int = 0
    single_image_cost: int = 0
    is_video_source: bool = False
    cover_confirmed: bool = False
    refs: list[str] = dataclass_field(default_factory=list)
    video_analysis: dict[str, Any] | None = None
    source_fingerprints: list[dict[str, Any]] = dataclass_field(default_factory=list)
    audio_result: dict[str, Any] | None = None
    audio_analyzed: bool = False
    gateway_invoked: bool = False
    gateway_call_recorded: bool = False
    gateway_result: dict[str, Any] | None = None
    provider_cost_detail: dict[str, Any] = dataclass_field(
        default_factory=lambda: {"provider_cost_status": "unavailable"}
    )
    pricing_snapshot: dict[str, Any] = dataclass_field(default_factory=dict)


def _validate_frozen_model_snapshot(state: _OperationRunState, service: Any) -> bool:
    assert state.operation is not None
    expected = dict(state.operation.model_snapshot or {})
    current = service._model_snapshot(state.model, state.runtime)
    snapshot_keys = (
        "model_config_id",
        "model_id",
        "provider",
        "gateway_endpoint_fingerprint",
        "gateway_format",
        "gateway_key_fingerprint",
    )
    if any(
        key in expected and expected.get(key) != current.get(key)
        for key in snapshot_keys
    ):
        service.fail_operation(
            state.operation_id,
            code="MODEL_CONFIG_CHANGED",
            error="反推提交后视觉模型配置已变更,请重新发起",
        )
        return False
    if (
        "gateway_endpoint_fingerprint" not in expected
        and "base_url" in expected
        and expected.get("base_url") != state.runtime.base_url
    ):
        service.fail_operation(
            state.operation_id,
            code="MODEL_CONFIG_CHANGED",
            error="反推提交后视觉模型配置已变更,请重新发起",
        )
        return False
    return True


def _prepare_operation_run(state: _OperationRunState, service: Any) -> bool:
    state.operation = _claim_operation(state.db, state.operation_id)
    if state.operation is None:
        return False
    state.context = dict(state.operation.request_context or {})
    try:
        state.body = ReverseOperationCreate.model_validate(state.context)
    except Exception:
        state.body = ReverseIn.model_validate(state.context)
    state.user = state.db.get(User, state.operation.user_id)
    if state.user is None:
        service.fail_operation(
            state.operation_id,
            code="USER_NOT_FOUND",
            error="用户不存在",
        )
        return False
    try:
        state.model = resolve_model_config(
            state.db,
            "vision",
            getattr(state.operation, "model_config_id", None),
            require_enabled=False,
        )
    except ModelConfigResolutionError as exc:
        service.fail_operation(
            state.operation_id,
            code="MODEL_UNAVAILABLE",
            error=str(exc),
        )
        return False
    state.runtime = runtime_config_for_model(state.model, "vision")
    service._assert_supported_vision_runtime(state.runtime)
    if not _validate_frozen_model_snapshot(state, service):
        return False

    assert state.body is not None
    state.is_video_source = reverse_source_resolution.is_video_source(state.body)
    state.preset = normalize_video_analysis_preset(
        state.body.video_analysis_preset
    )
    state.gateway_target = state.body.target
    state.pricing_snapshot = dict(state.operation.pricing_snapshot or {})
    quoted_visual_cost = state.pricing_snapshot.get("visual_cost")
    state.visual_cost = int(
        quoted_visual_cost
        if quoted_visual_cost is not None
        else service._reverse_cost_for_model(
            state.model,
            state.body.target,
            preset=state.preset,
        )
    )
    state.audio_surcharge = max(
        0,
        int(state.pricing_snapshot.get("audio_surcharge") or 0),
    )
    state.real_cost = state.visual_cost
    quoted_single_image_cost = state.pricing_snapshot.get("single_image_cost")
    state.single_image_cost = int(
        quoted_single_image_cost
        if quoted_single_image_cost is not None
        else service._reverse_cost_for_model(state.model, "image")
    )
    if state.is_video_source and state.body.target != "video":
        service.fail_operation(
            state.operation_id,
            code="INVALID_SOURCE",
            error="视频素材仅支持视频反推",
        )
        return False
    phase = "extracting_frames" if state.is_video_source else "resolving_asset"
    return _set_phase(state.db, state.operation_id, phase, 15)


def _resolve_confirmed_cover(state: _OperationRunState) -> None:
    assert state.operation is not None
    assert state.user is not None
    fallback = str(state.context.get("fallback_image") or "").strip()
    ref, content_hash = reverse_source_resolution.gateway_ref_with_content_hash(
        state.db,
        state.user,
        fallback,
    )
    state.refs = [ref] if ref else []
    if ref and content_hash:
        state.source_fingerprints = [
            reverse_lineage.build_source_fingerprint(
                source_index=1,
                content_hash=content_hash,
                locator=fallback,
            )
        ]
    previous = (
        state.operation.result
        if isinstance(state.operation.result, dict)
        else {}
    )
    previous_analysis = (
        previous.get("video_analysis")
        if isinstance(previous, dict)
        else None
    )
    state.video_analysis = (
        dict(previous_analysis)
        if isinstance(previous_analysis, dict)
        else {}
    )
    state.video_analysis.update(
        {
            "analysis_mode": "cover_fallback",
            "degraded_reason": "用户已确认改用封面单帧进行运动设计",
        }
    )
    state.gateway_target = "image_to_video"
    state.real_cost = state.single_image_cost


def _collect_operation_refs(state: _OperationRunState) -> bool:
    assert state.body is not None
    assert state.user is not None
    frame_budget = 1
    if state.is_video_source:
        # Pass the preset capacity, not the duration-derived initial target.
        # The sampler computes the duration target itself, then spends spare
        # capacity when scene detection shows that each shot needs more frames.
        frame_budget = max_frame_count(state.preset)
    try:
        collected = reverse_source_resolution.collect_refs(
            state.body,
            state.db,
            state.user,
            frame_budget=frame_budget,
            video_preset=state.preset,
            gateway_mock=(
                settings.mock_mode
                or (
                    state.runtime.source == "env"
                    and settings.effective_mock_mode
                )
            ),
            include_source_fingerprints=True,
            gateway_ref_resolver=reverse_source_resolution.gateway_ref,
            gateway_ref_with_hash_resolver=(
                reverse_source_resolution.gateway_ref_with_content_hash
            ),
        )
    except HTTPException as exc:
        if state.is_video_source and exc.status_code == 400:
            _pause_for_cover(
                state.db,
                state.operation_id,
                video_analysis=None,
                reason=str(exc.detail),
            )
            return False
        raise
    if isinstance(collected, tuple) and len(collected) == 3:
        state.refs, state.video_analysis, state.source_fingerprints = collected
    elif isinstance(collected, tuple) and len(collected) == 2:
        state.refs, state.video_analysis = collected
        state.source_fingerprints = _fallback_reference_fingerprints(state.refs)
    else:
        state.refs = list(collected)
        state.video_analysis = None
        state.source_fingerprints = _fallback_reference_fingerprints(state.refs)
    if (
        state.is_video_source
        and isinstance(state.video_analysis, dict)
        and state.video_analysis.get("analysis_mode") == "cover_fallback"
    ):
        _pause_for_cover(
            state.db,
            state.operation_id,
            video_analysis=state.video_analysis,
            reason=str(
                state.video_analysis.get("degraded_reason")
                or "视频抽帧不可用"
            ),
        )
        return False
    if state.body.target == "video" and not state.is_video_source:
        state.gateway_target = "image_to_video"
        state.real_cost = state.single_image_cost
    return True


def _attach_operation_audio(state: _OperationRunState, service: Any) -> bool:
    assert state.body is not None
    assert state.user is not None
    should_analyze = (
        state.is_video_source
        and bool(getattr(state.body, "include_audio", False))
        and not state.cover_confirmed
        and isinstance(state.video_analysis, dict)
    )
    if not should_analyze:
        return True
    if not _set_phase(state.db, state.operation_id, "analyzing_audio", 35):
        return False
    state.audio_result = service._audio_analysis_for_request(
        state.body,
        state.db,
        state.user,
    )
    state.audio_analyzed = _has_timestamped_audio_evidence(
        state.audio_result
    )
    assert state.video_analysis is not None
    source = (
        dict(state.video_analysis.get("source"))
        if isinstance(state.video_analysis.get("source"), dict)
        else {}
    )
    source["audio_analyzed"] = state.audio_analyzed
    if state.audio_analyzed:
        source["has_audio"] = True
        state.real_cost = state.visual_cost + state.audio_surcharge
    state.video_analysis["source"] = source
    state.video_analysis["audio"] = state.audio_result
    return True


def _resolve_operation_assets(state: _OperationRunState, service: Any) -> bool:
    state.cover_confirmed = bool(state.context.get("cover_confirmed"))
    if state.is_video_source and state.cover_confirmed:
        _resolve_confirmed_cover(state)
    elif not _collect_operation_refs(state):
        return False
    if not _attach_operation_audio(state, service):
        return False
    if not state.refs:
        raise ReverseOperationInvalid("素材解析后没有可用参考帧")
    return True


def _operation_gateway_kwargs(state: _OperationRunState) -> dict[str, Any]:
    assert state.operation is not None
    assert state.body is not None
    kwargs: dict[str, Any] = {"target": state.gateway_target}
    if _gateway_accepts("gateway_config"):
        kwargs["gateway_config"] = state.runtime
    if (
        state.video_analysis is not None
        and _gateway_accepts("video_analysis")
    ):
        kwargs["video_analysis"] = state.video_analysis
    reference_context = (
        state.video_analysis.get("reference_context")
        if isinstance(state.video_analysis, dict)
        and isinstance(state.video_analysis.get("reference_context"), list)
        else None
    )
    if (
        reference_context is not None
        and _gateway_accepts("reference_context")
    ):
        kwargs["reference_context"] = reference_context
    if (
        state.gateway_target == "video"
        and state.is_video_source
        and _gateway_accepts("require_video_frame_coverage")
    ):
        kwargs["require_video_frame_coverage"] = True
    templates = (
        (state.operation.template_snapshot or {}).get("templates")
        if isinstance(state.operation.template_snapshot, dict)
        else None
    )
    if (
        isinstance(templates, dict)
        and isinstance(templates.get(state.gateway_target), str)
        and _gateway_accepts("template_override")
    ):
        kwargs["template_override"] = templates[state.gateway_target]
    if _gateway_accepts("before_repair"):
        kwargs["before_repair"] = lambda: _set_phase(
            state.db,
            state.operation_id,
            "repairing",
            70,
        )
    if _gateway_accepts("audit_context"):
        kwargs["audit_context"] = {
            "operation_id": state.operation_id,
            "preset": state.preset,
            "cost_credits": state.real_cost,
            "audio_status": (
                state.audio_result.get("status")
                if isinstance(state.audio_result, dict)
                else "not_requested"
            ),
        }
    return kwargs


def _invoke_operation_gateway(
    state: _OperationRunState,
    service: Any,
) -> dict[str, Any] | None:
    assert state.model is not None
    if not _set_phase(state.db, state.operation_id, "calling_model", 45):
        return None
    kwargs = _operation_gateway_kwargs(state)
    state.provider_cost_detail = service._provider_cost_call_detail(
        state.pricing_snapshot,
        contract_target=state.gateway_target,
        preset=state.preset,
        audio_evidence=state.audio_analyzed,
    )
    state.gateway_invoked = True
    raw_result = gateway.reverse_prompt(
        state.refs,
        state.model.model_id,
        **kwargs,
    )
    state.gateway_result = (
        raw_result if isinstance(raw_result, dict) else None
    )
    return validate_reverse_result(
        raw_result,
        state.gateway_target,
        source_count=len(state.refs),
    )


def _attach_video_evidence(
    state: _OperationRunState,
    result: dict[str, Any],
) -> None:
    assert state.video_analysis is not None
    persisted = dict(state.video_analysis)
    persisted.pop("reference_context", None)
    result["video_analysis"] = _merge_video_analysis_result(result, persisted)
    frame_rows = (
        persisted.get("sampled_frames")
        if isinstance(persisted.get("sampled_frames"), list)
        else []
    )
    independent = video_evidence_analysis.analyze_video_evidence(
        state.refs[: len(frame_rows)],
        frame_rows,
    )
    result["video_analysis"]["evidence_analyzers"] = independent
    shots = result["video_analysis"].get("shots")
    if not isinstance(shots, list):
        return
    enriched_shots = video_evidence_analysis.attach_evidence_to_shots(
        shots,
        independent,
    )
    result["video_analysis"]["shots"] = (
        video_evidence_analysis.attach_audio_evidence_to_shots(
            enriched_shots,
            result["video_analysis"].get("audio"),
        )
    )
    if state.gateway_target == "video":
        _finalize_evidence_bound_video_result(result)


def _attach_image_evidence(
    state: _OperationRunState,
    result: dict[str, Any],
) -> None:
    independent = image_evidence_analysis.analyze_image_sources(
        state.refs,
        vlm_evidence=(
            result.get("image_evidence")
            if isinstance(result.get("image_evidence"), list)
            else []
        ),
        source_fingerprints=state.source_fingerprints,
    )
    result["image_evidence"] = independent["evidence"]
    result["image_evidence_analyzers"] = independent["analyzers"]
    result["image_evidence_contract_version"] = independent[
        "contract_version"
    ]
    _apply_image_ocr_gate_to_text(
        result,
        independent["evidence"],
        state.gateway_target,
    )


def _postprocess_operation_result(
    state: _OperationRunState,
    result: dict[str, Any],
    service: Any,
) -> None:
    if state.is_video_source and state.video_analysis is not None:
        _attach_video_evidence(state, result)
    elif state.gateway_target in IMAGE_EVIDENCE_TARGETS:
        _attach_image_evidence(state, result)
    service.assert_text_allowed(
        state.db,
        result.get("structured"),
        result.get("final_text"),
    )


def _complete_operation_run(
    state: _OperationRunState,
    result: dict[str, Any],
    service: Any,
) -> bool:
    assert state.operation is not None
    assert state.model is not None
    assert state.body is not None
    usage.record_call(
        state.db,
        kind="reverse",
        model_id=state.model.model_id,
        user_id=state.operation.user_id,
        model_config_id=getattr(
            state.operation,
            "model_config_id",
            None,
        ),
        status="ok",
        latency_ms=(state.gateway_result or {}).get("latency_ms"),
        usage=(state.gateway_result or {}).get("usage"),
        detail={
            "operation_id": state.operation_id,
            "target": state.body.target,
            "contract_target": state.gateway_target,
            "preset": state.preset,
            "frames": len(state.refs),
            "cost_credits": state.real_cost,
            **state.provider_cost_detail,
            "phase": "gateway_completed",
            "error_code": None,
            "repair_attempted": bool(
                (state.gateway_result or {}).get("repair_attempted")
            ),
            "audio_status": (
                state.audio_result.get("status")
                if isinstance(state.audio_result, dict)
                else "not_requested"
            ),
        },
    )
    state.gateway_call_recorded = True
    if not _set_phase(state.db, state.operation_id, "settling", 90):
        return False
    finished = _finish_success(
        state.db,
        state.operation_id,
        result=result,
        provider_result=state.gateway_result,
        source_fingerprints=state.source_fingerprints,
        reference_count=len(state.refs),
        real_cost=state.real_cost,
    )
    if finished is None:
        return False
    service._remember_history(finished, result)
    return True


def _record_run_gateway_failure(
    state: _OperationRunState,
    *,
    phase: str,
    error_code: str,
    error: str,
    repair_attempted: bool,
) -> None:
    if (
        not state.gateway_invoked
        or state.gateway_call_recorded
        or state.operation is None
        or state.user is None
    ):
        return
    target = (
        state.body.target
        if state.body is not None
        else state.operation.target
    )
    _record_gateway_failure(
        state.db,
        operation_id=state.operation_id,
        model_id=getattr(state.model, "model_id", None),
        user_id=state.user.id,
        target=target,
        contract_target=state.gateway_target,
        preset=state.preset,
        cost_credits=state.real_cost,
        provider_cost_detail=state.provider_cost_detail,
        phase=phase,
        error_code=error_code,
        error=error,
        repair_attempted=repair_attempted,
        result=state.gateway_result,
    )


def _http_operation_failure(exc: HTTPException, invoked: bool) -> tuple[str, str]:
    status_code = int(exc.status_code)
    if invoked and status_code >= 500:
        return "GATEWAY_ERROR", "calling_model"
    if invoked:
        return "CONTENT_SAFETY_BLOCKED", "validating_output"
    if status_code == 404:
        return "ASSET_NOT_FOUND", "resolving_asset"
    return "REQUEST_REJECTED", "resolving_asset"


def _handle_operation_run_failure(
    state: _OperationRunState,
    exc: Exception,
    service: Any,
) -> None:
    if isinstance(exc, HTTPException):
        error_code, phase = _http_operation_failure(
            exc,
            state.gateway_invoked,
        )
        error = str(exc.detail)
        repair_attempted = bool(
            (state.gateway_result or {}).get("repair_attempted")
        )
    elif isinstance(exc, gateway.GatewayError):
        error_code = str(exc.error_code or "GATEWAY_ERROR")
        phase = str(exc.phase or "calling_model")
        error = str(exc)
        repair_attempted = (
            phase == "repairing"
            or bool((state.gateway_result or {}).get("repair_attempted"))
        )
    elif isinstance(exc, ReverseOperationInvalid):
        service.fail_operation(
            state.operation_id,
            code="INVALID_OPERATION",
            error=str(exc),
        )
        return
    else:
        _log_operation_event(
            "reverse_operation_crashed",
            operation_id=state.operation_id,
            status=getattr(state.operation, "status", None),
            phase=getattr(state.operation, "phase", None),
            error_code="INTERNAL_ERROR",
            level=logging.ERROR,
        )
        log.exception(
            "reverse operation crash traceback operation_id=%s",
            state.operation_id,
        )
        error_code = "INTERNAL_ERROR"
        phase = str(
            getattr(exc, "reverse_phase", None) or "calling_model"
        )
        error = str(exc)
        repair_attempted = phase == "repairing"
    _record_run_gateway_failure(
        state,
        phase=phase,
        error_code=error_code,
        error=error,
        repair_attempted=repair_attempted,
    )
    service.fail_operation(
        state.operation_id,
        code=error_code,
        error=error,
    )


def run_operation(operation_id: int) -> None:
    """Run one operation. Redelivery is harmless because claiming is conditional."""
    from . import reverse_operations as service

    state = _OperationRunState(
        operation_id=operation_id,
        db=SessionLocal(),
    )
    try:
        if not _prepare_operation_run(state, service):
            return
        if not _resolve_operation_assets(state, service):
            return
        result = _invoke_operation_gateway(state, service)
        if result is None:
            return
        _postprocess_operation_result(state, result, service)
        _complete_operation_run(state, result, service)
    except Exception as exc:  # noqa: BLE001
        _handle_operation_run_failure(state, exc, service)
    finally:
        state.db.close()



def _fail_stale_running(
    operation_id: int,
    *,
    cutoff,
    now,
) -> bool:
    from . import reverse_operations as service

    return _reverse_reaper_module._fail_stale_running(
        operation_id,
        cutoff=cutoff,
        now=now,
        refund_locked=service._refund_locked,
    )


def reap_operations() -> dict[str, int]:
    from . import reverse_operations as service

    return _reverse_reaper_module.reap_operations(
        refund_locked=service._refund_locked,
        enqueue_operation=service.enqueue_operation,
    )


_install_assignment_forwarding(
    __name__,
    (
        _reverse_reaper_module,
        _reverse_result_processing_module,
        _reverse_settlement_module,
    ),
)
