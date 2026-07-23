"""Durable, immutable execution plans for reproduction remediations.

The public remediation API only creates a plan.  Generation is deliberately
kept in the normal quote/submit flow; this module supplies the server-side
binding checks that prevent a client from changing a planned request between
quote and execution.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    GenAsset,
    GenTask,
    ModelConfig,
    ReproductionAssessment,
    ReproductionCorrection,
    ReproductionFinding,
    ReproductionRemediation,
    ReverseResultRevision,
    WorkflowRun,
)
from ..reproduction_schemas import (
    ReproductionCorrectionCreateIn,
    ReproductionRemediationCreateIn,
)
from ..schemas import GenerationQuoteIn
from ..workflow_schemas import ToolRunCreateIn
from . import asset_refs, reproduction_assessment, reverse_lineage, reverse_operations, storage
from .user_assets import AssetNotFound, InvalidAssetRef, ResolvedAsset, resolve_asset_ref

log = logging.getLogger(__name__)


class ReproductionRemediationError(ValueError):
    status_code = 422
    code = "REPRODUCTION_REMEDIATION_INVALID"


class ReproductionRemediationNotFound(ReproductionRemediationError):
    status_code = 404
    code = "REPRODUCTION_REMEDIATION_NOT_FOUND"


class ReproductionRemediationConflict(ReproductionRemediationError):
    status_code = 409
    code = "REPRODUCTION_REMEDIATION_CONFLICT"


_TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "canceled"})
_ACTIVE_TASK_STATUSES = frozenset({"queued", "running", "needs_review"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _request_fingerprint(body: ReproductionRemediationCreateIn) -> str:
    return _hash(body.model_dump(mode="json", exclude={"idempotency_key"}))


def _owned_assessment(
    db: Session, *, assessment_id: int, user_id: int
) -> ReproductionAssessment:
    row = db.scalar(
        select(ReproductionAssessment).where(
            ReproductionAssessment.id == int(assessment_id),
            ReproductionAssessment.user_id == int(user_id),
        )
    )
    if row is None:
        raise ReproductionRemediationNotFound("复刻度评估不存在")
    return row


def _owned_remediation(
    db: Session, *, remediation_id: int, user_id: int
) -> ReproductionRemediation:
    row = db.scalar(
        select(ReproductionRemediation).where(
            ReproductionRemediation.id == int(remediation_id),
            ReproductionRemediation.user_id == int(user_id),
        )
    )
    if row is None:
        raise ReproductionRemediationNotFound("纠偏执行计划不存在")
    return row


def _resolve_asset(
    db: Session, *, user_id: int, asset_ref: str
) -> ResolvedAsset:
    try:
        return resolve_asset_ref(db, int(user_id), asset_ref)
    except (AssetNotFound, InvalidAssetRef) as exc:
        raise ReproductionRemediationNotFound("资产不存在") from exc


def _asset_media_type(resolved: ResolvedAsset) -> str:
    if resolved.origin == "generated":
        return str(resolved.row.type)
    return "video" if str(resolved.row.key).startswith("upload_video/") else "image"


def _asset_url(resolved: ResolvedAsset) -> str:
    if resolved.origin == "generated":
        assert isinstance(resolved.row, GenAsset)
        url = resolved.row.hd_url if resolved.row.unlocked and resolved.row.hd_url else resolved.row.preview_url
        if url:
            return str(url)
    else:
        url = storage.public_url(str(resolved.row.key))
        if url:
            return str(url)
    raise ReproductionRemediationError("执行计划缺少可用的素材地址")


def _valid_bbox(value: Any) -> bool:
    """Require a non-empty normalized rectangular region for image inpaint."""
    if not isinstance(value, dict):
        return False
    aliases = (
        ("x", "y", "width", "height"),
        ("left", "top", "width", "height"),
        ("x", "y", "w", "h"),
    )
    for x_key, y_key, width_key, height_key in aliases:
        try:
            x = float(value[x_key])
            y = float(value[y_key])
            width = float(value[width_key])
            height = float(value[height_key])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 and 0 < height <= 1:
            return x + width <= 1.000001 and y + height <= 1.000001
    return False


def _model_for_mode(db: Session, *, model_config_id: int, media_type: str) -> ModelConfig:
    row = db.get(ModelConfig, int(model_config_id))
    if row is None or not row.enabled or str(row.use) != media_type:
        raise ReproductionRemediationError("所选生成模型不可用或与媒体类型不匹配")
    return row


def _stable_revision_prompt(revision: ReverseResultRevision) -> dict[str, Any]:
    payload = deepcopy(revision.payload) if isinstance(revision.payload, dict) else {}
    structured = payload.get("structured")
    prompt: dict[str, Any] = deepcopy(structured) if isinstance(structured, dict) else {}
    final_text = payload.get("final_text")
    if isinstance(final_text, str) and final_text.strip():
        prompt["final_text"] = final_text.strip()
    if not prompt:
        prompt["final_text"] = "按已确认的复刻纠偏方案生成"
    negative_prompt = payload.get("negative_prompt")
    if isinstance(negative_prompt, str) and negative_prompt.strip():
        prompt["negative_prompt"] = negative_prompt.strip()
    return prompt


def _plan_request(
    *,
    remediation_id: int,
    item_id: str,
    mode: str,
    model_config_id: int,
    source_url: str,
    media_type: str,
    operation_id: int,
    applied_revision_id: int,
    prompt: dict[str, Any],
    params: dict[str, Any],
    shot_id: str | None,
    time_range: dict[str, Any] | None,
) -> dict[str, Any]:
    request_params = deepcopy(params)
    if mode == "image_inpaint":
        request_params.setdefault("reference_image_url", source_url)
    if shot_id is not None:
        request_params["remediation_shot_id"] = shot_id
    if time_range is not None:
        request_params["remediation_time_range"] = deepcopy(time_range)
    raw = {
        "client_request_id": f"remediation-{remediation_id}-{item_id}",
        "reproduction_remediation_id": remediation_id,
        "reproduction_plan_item_id": item_id,
        "reverse_operation_id": operation_id,
        "reverse_revision_id": applied_revision_id,
        "source_asset_url": source_url,
        "source_type": media_type,
        "category": media_type,
        "stage": "preview",
        "prompt": deepcopy(prompt),
        "params": request_params,
        "model_config_id": model_config_id,
    }
    # Persist the canonical request model shape.  Later validation receives
    # ``GenerateIn.model_dump(exclude={"quote_id"})`` and can compare exactly.
    try:
        return GenerationQuoteIn.model_validate(raw).model_dump(mode="json", exclude_none=True)
    except Exception as exc:  # Schema changes should fail plan creation loudly.
        raise ReproductionRemediationError(f"无法构造可执行的纠偏请求: {exc}") from exc


def _execution_item(snapshot: dict[str, Any], task: GenTask | None, asset: GenAsset | None) -> dict[str, Any]:
    if task is None:
        status = "planned"
    else:
        status = str(task.status)
    return {
        "item_id": str(snapshot["item_id"]),
        "kind": snapshot["kind"],
        "shot_id": snapshot.get("shot_id"),
        "finding_ids": [int(value) for value in snapshot.get("finding_ids") or []],
        "status": status,
        "generation_task_id": int(task.id) if task is not None else None,
        "asset_id": int(asset.id) if asset is not None else None,
        "asset_ref": f"g.{int(asset.id)}" if asset is not None else None,
        "error_code": "GENERATION_FAILED" if task is not None and task.status == "failed" else None,
        "error": task.error if task is not None and task.status == "failed" else None,
    }


def serialize_remediation(db: Session, row: ReproductionRemediation) -> dict[str, Any]:
    correction = db.get(ReproductionCorrection, int(row.correction_id))
    if correction is None:
        raise ReproductionRemediationConflict("纠偏记录已损坏")
    snapshot = deepcopy(row.plan_snapshot or [])
    if _hash(snapshot) != str(row.plan_hash):
        raise ReproductionRemediationConflict("纠偏计划快照校验失败")
    task_ids = [int(value) for value in row.generation_task_ids or []]
    tasks = {
        int(task.id): task
        for task in db.scalars(select(GenTask).where(GenTask.id.in_(task_ids))).all()
    } if task_ids else {}
    assets = {
        int(asset.task_id): asset
        for asset in db.scalars(select(GenAsset).where(GenAsset.task_id.in_(task_ids))).all()
        if asset.task_id is not None and int(asset.user_id) == int(row.user_id)
    } if task_ids else {}
    plan_items = [
        _execution_item(item, tasks.get(task_ids[index]) if index < len(task_ids) else None,
                        assets.get(task_ids[index]) if index < len(task_ids) else None)
        for index, item in enumerate(snapshot)
    ]
    final_asset = db.get(GenAsset, int(row.final_asset_id)) if row.final_asset_id is not None else None
    return {
        "id": int(row.id),
        "assessment_id": int(row.assessment_id),
        "correction_id": int(row.correction_id),
        "user_id": int(row.user_id),
        "parent_remediation_id": int(row.parent_remediation_id) if row.parent_remediation_id is not None else None,
        "idempotency_key": row.idempotency_key,
        "request_fingerprint": row.request_fingerprint,
        "mode": row.mode,
        "status": row.status,
        "selected_finding_ids": [int(value) for value in row.selected_finding_ids or []],
        "selected_shot_ids": list(row.selected_shot_ids or []),
        "source_asset_ref": row.source_asset_ref,
        "target_asset_ref": row.target_asset_ref,
        "parent_revision_id": int(correction.parent_revision_id),
        "applied_revision_id": int(row.applied_revision_id),
        "plan_snapshot": snapshot,
        "plan_items": plan_items,
        "plan_hash": row.plan_hash,
        "video_composition": deepcopy(row.video_composition),
        "generation_task_ids": task_ids,
        "composition_tool_run_id": int(row.composition_tool_run_id) if row.composition_tool_run_id is not None else None,
        "composition_workflow_run_id": int(row.composition_workflow_run_id) if row.composition_workflow_run_id is not None else None,
        "final_asset_id": int(row.final_asset_id) if row.final_asset_id is not None else None,
        "final_asset_ref": f"g.{int(final_asset.id)}" if final_asset is not None else None,
        "successor_assessment_id": int(row.successor_assessment_id) if row.successor_assessment_id is not None else None,
        "auto_reassess": bool(row.auto_reassess),
        "error_code": row.error_code,
        "error": row.error,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def create_remediation(
    db: Session, *, assessment_id: int, user_id: int, body: ReproductionRemediationCreateIn
) -> tuple[dict[str, Any], bool]:
    assessment = _owned_assessment(db, assessment_id=assessment_id, user_id=user_id)
    fingerprint = _request_fingerprint(body)
    replay = db.scalar(select(ReproductionRemediation).where(
        ReproductionRemediation.assessment_id == int(assessment_id),
        ReproductionRemediation.idempotency_key == body.idempotency_key,
    ))
    if replay is not None:
        if replay.request_fingerprint != fingerprint:
            raise ReproductionRemediationConflict("幂等 key 已用于不同纠偏执行计划")
        return serialize_remediation(db, replay), False
    if assessment.status not in reproduction_assessment.CORRECTABLE_STATUSES:
        raise ReproductionRemediationConflict("只能为已完成或部分完成的评估创建纠偏计划")
    if assessment.reverse_operation_id is None:
        raise ReproductionRemediationConflict("评估没有可用的反推血缘")
    expected_mode = "image_inpaint" if assessment.media_type == "image" else "video_shot_regenerate"
    if body.mode != expected_mode:
        raise ReproductionRemediationError("纠偏模式与评估媒体类型不匹配")
    _model_for_mode(db, model_config_id=body.model_config_id, media_type=assessment.media_type)
    source = _resolve_asset(db, user_id=user_id, asset_ref=assessment.source_asset_ref)
    target = _resolve_asset(db, user_id=user_id, asset_ref=assessment.generated_asset_ref)
    if _asset_media_type(source) != assessment.media_type or _asset_media_type(target) != assessment.media_type:
        raise ReproductionRemediationConflict("评估关联资产类型已变化")
    findings = list(db.scalars(select(ReproductionFinding).where(
        ReproductionFinding.assessment_id == int(assessment_id),
        ReproductionFinding.id.in_(body.selected_finding_ids),
    )).all())
    if {int(item.id) for item in findings} != set(body.selected_finding_ids):
        raise ReproductionRemediationError("所选 finding 不属于当前评估")
    findings.sort(key=lambda item: body.selected_finding_ids.index(int(item.id)))
    if body.mode == "image_inpaint":
        if any(not _valid_bbox(item.bbox) for item in findings):
            raise ReproductionRemediationError("图片纠偏的每个 finding 必须包含有效的归一化 bbox")
    else:
        finding_shots = {str(item.shot_id).strip() for item in findings if isinstance(item.shot_id, str) and item.shot_id.strip()}
        if len(finding_shots) != len({item.shot_id for item in findings}):
            raise ReproductionRemediationError("视频纠偏的每个 finding 必须包含稳定的 shot_id")
        if not body.selected_shot_ids or set(body.selected_shot_ids) != finding_shots:
            raise ReproductionRemediationError("selected_shot_ids 必须完整且准确覆盖所选 finding 的 shot_id")
    parent = db.get(ReverseResultRevision, int(body.parent_revision_id))
    if parent is None or int(parent.user_id) != int(user_id) or int(parent.operation_id) != int(assessment.reverse_operation_id):
        raise ReproductionRemediationConflict("纠偏父版本不可用")
    if body.parent_remediation_id is not None:
        predecessor = _owned_remediation(db, remediation_id=body.parent_remediation_id, user_id=user_id)
        if int(predecessor.assessment_id) != int(assessment_id):
            raise ReproductionRemediationConflict("父纠偏计划不属于当前评估")
    # The correction and its executable remediation are one unit of work.  The
    # namespaced key avoids colliding with direct correction requests while
    # allowing the outer transaction to roll back both records on plan failure.
    correction_idempotency_key = "rem:" + hashlib.sha256(
        f"{assessment_id}:{body.idempotency_key}".encode()
    ).hexdigest()
    correction_body = ReproductionCorrectionCreateIn(
        idempotency_key=correction_idempotency_key,
        parent_revision_id=body.parent_revision_id,
        selected_finding_ids=body.selected_finding_ids,
        structured_patch={},
        prompt_patch=body.prompt_patch,
        negative_prompt_patch=body.negative_prompt_patch,
        mask_patch=({"regions": [deepcopy(item.bbox) for item in findings]} if body.mode == "image_inpaint" else None),
        apply=True,
    )
    try:
        correction_data, _ = reproduction_assessment.create_correction(
            db,
            assessment_id=assessment_id,
            user_id=user_id,
            body=correction_body,
            commit=False,
        )
    except reproduction_assessment.ReproductionAssessmentNotFound as exc:
        raise ReproductionRemediationNotFound(str(exc)) from exc
    except reproduction_assessment.ReproductionAssessmentConflict as exc:
        raise ReproductionRemediationConflict(str(exc)) from exc
    except reproduction_assessment.ReproductionAssessmentError as exc:
        raise ReproductionRemediationError(str(exc)) from exc
    applied = correction_data.get("applied_revision") or {}
    applied_revision_id = int(applied.get("id") or 0)
    if applied_revision_id <= 0:
        raise ReproductionRemediationConflict("纠偏版本未成功应用")
    applied_revision = db.get(ReverseResultRevision, applied_revision_id)
    if applied_revision is None:
        raise ReproductionRemediationConflict("已应用纠偏版本不存在")
    correction = db.get(ReproductionCorrection, int(correction_data["id"]))
    if correction is None:
        raise ReproductionRemediationConflict("纠偏记录不存在")
    row = ReproductionRemediation(
        assessment_id=int(assessment_id), correction_id=int(correction.id), user_id=int(user_id),
        parent_remediation_id=body.parent_remediation_id, idempotency_key=body.idempotency_key,
        request_fingerprint=fingerprint, mode=body.mode, status="planned",
        selected_finding_ids=[int(item) for item in body.selected_finding_ids],
        selected_shot_ids=list(body.selected_shot_ids), source_asset_ref=assessment.source_asset_ref,
        target_asset_ref=assessment.generated_asset_ref, applied_revision_id=applied_revision_id,
        plan_snapshot=[], plan_hash=_hash([]), video_composition=deepcopy(body.video_composition),
        generation_task_ids=[], auto_reassess=bool(body.auto_reassess),
    )
    db.add(row)
    db.flush()
    target_url = _asset_url(target)
    prompt = _stable_revision_prompt(applied_revision)
    by_shot: dict[str, list[ReproductionFinding]] = {}
    for finding in findings:
        by_shot.setdefault(str(finding.shot_id) if finding.shot_id is not None else "", []).append(finding)
    snapshot: list[dict[str, Any]] = []
    if body.mode == "image_inpaint":
        item_id = "image-inpaint-1"
        snapshot.append({
            "item_id": item_id, "kind": body.mode, "shot_id": None,
            "finding_ids": [int(item.id) for item in findings],
            "request": _plan_request(remediation_id=int(row.id), item_id=item_id, mode=body.mode,
                model_config_id=body.model_config_id, source_url=target_url, media_type=assessment.media_type,
                operation_id=int(assessment.reverse_operation_id), applied_revision_id=applied_revision_id,
                prompt=prompt, params=body.params, shot_id=None, time_range=None),
        })
    else:
        for index, shot_id in enumerate(body.selected_shot_ids, start=1):
            item_id = f"shot-{index}"
            shot_findings = by_shot[shot_id]
            finding_contexts = [
                {
                    "finding_id": int(finding.id),
                    "dimension": finding.dimension,
                    "kind": finding.kind,
                    "severity": finding.severity,
                    "confidence": finding.confidence,
                    "instruction": finding.message,
                    "time_range": deepcopy(finding.time_range),
                    "shot_id": finding.shot_id,
                }
                for finding in shot_findings
            ]
            prepared = reverse_operations.prepare_shot_generation(
                db,
                operation_id=int(assessment.reverse_operation_id),
                user_id=int(user_id),
                shot_id=shot_id,
                revision_id=applied_revision_id,
                client_request_id=f"remediation-{int(row.id)}-{item_id}",
                model_config_id=body.model_config_id,
                params=body.params,
                finding_contexts=finding_contexts,
                prompt_patch=body.prompt_patch,
                negative_prompt_patch=body.negative_prompt_patch,
                reproduction_remediation_id=int(row.id),
                reproduction_plan_item_id=item_id,
            )
            try:
                request = GenerationQuoteIn.model_validate(prepared["request"]).model_dump(
                    mode="json", exclude_none=True
                )
            except Exception as exc:
                raise ReproductionRemediationError(f"无法构造可执行的视频镜头请求: {exc}") from exc
            snapshot.append({
                "item_id": item_id, "kind": body.mode, "shot_id": shot_id,
                "finding_ids": [int(item.id) for item in shot_findings],
                "request": request,
            })
    row.plan_snapshot = snapshot
    row.plan_hash = _hash(snapshot)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError as exc:
        db.rollback()
        replay = db.scalar(select(ReproductionRemediation).where(
            ReproductionRemediation.assessment_id == int(assessment_id),
            ReproductionRemediation.idempotency_key == body.idempotency_key,
        ))
        if replay is not None and replay.request_fingerprint == fingerprint:
            return serialize_remediation(db, replay), False
        raise ReproductionRemediationConflict("纠偏计划已被并发更新") from exc
    return serialize_remediation(db, row), True


def get_remediation(db: Session, *, assessment_id: int, remediation_id: int, user_id: int) -> dict[str, Any]:
    row = _owned_remediation(db, remediation_id=remediation_id, user_id=user_id)
    if int(row.assessment_id) != int(assessment_id):
        raise ReproductionRemediationNotFound("纠偏执行计划不存在")
    return serialize_remediation(db, row)


def list_remediations(
    db: Session, *, assessment_id: int, user_id: int, limit: int, offset: int
) -> dict[str, Any]:
    _owned_assessment(db, assessment_id=assessment_id, user_id=user_id)
    filters = [ReproductionRemediation.assessment_id == int(assessment_id), ReproductionRemediation.user_id == int(user_id)]
    total = int(db.scalar(select(func.count()).select_from(ReproductionRemediation).where(*filters)) or 0)
    rows = list(db.scalars(select(ReproductionRemediation).where(*filters).order_by(
        ReproductionRemediation.created_at.desc(), ReproductionRemediation.id.desc()
    ).limit(limit).offset(offset)).all())
    return {"items": [serialize_remediation(db, row) for row in rows], "total": total, "limit": limit, "offset": offset}


def validate_generation_request(
    db: Session, *, user_id: int, remediation_id: int, plan_item_id: str, request_payload: dict[str, Any]
) -> dict[str, Any]:
    """Return stable remediation context only when a submitted request is the plan snapshot."""
    row = _owned_remediation(db, remediation_id=remediation_id, user_id=user_id)
    snapshot = deepcopy(row.plan_snapshot or [])
    if _hash(snapshot) != str(row.plan_hash):
        raise ReproductionRemediationConflict("纠偏计划快照校验失败")
    item = next((value for value in snapshot if value.get("item_id") == plan_item_id), None)
    if item is None:
        raise ReproductionRemediationNotFound("纠偏计划项不存在")
    expected = item.get("request")
    if not isinstance(expected, dict) or not isinstance(request_payload, dict):
        raise ReproductionRemediationError("纠偏生成请求格式无效")
    if _canonical_json(expected) != _canonical_json(request_payload):
        raise ReproductionRemediationConflict("生成请求与已确认纠偏计划不一致")
    item_index = snapshot.index(item)
    task_ids = [int(value) for value in row.generation_task_ids or []]
    if item_index < len(task_ids):
        bound_task = db.get(GenTask, task_ids[item_index])
        if (
            bound_task is None
            or int(bound_task.user_id) != int(user_id)
            or bound_task.client_request_id != expected.get("client_request_id")
        ):
            raise ReproductionRemediationConflict("纠偏计划项已绑定其他生成任务")
    elif row.status not in {"planned", "generating"}:
        raise ReproductionRemediationConflict("纠偏计划已结束，不能创建新的生成任务")
    return {
        "remediation_id": int(row.id), "assessment_id": int(row.assessment_id),
        "plan_item_id": str(item["item_id"]), "mode": row.mode,
        "source_asset_ref": row.source_asset_ref, "target_asset_ref": row.target_asset_ref,
        "applied_revision_id": int(row.applied_revision_id),
        "finding_ids": [int(value) for value in item.get("finding_ids") or []],
        "shot_id": item.get("shot_id"), "plan_hash": row.plan_hash,
    }


def resolve_generation_request(**kwargs) -> dict[str, Any]:
    """Compatibility name for generation callers; see validate_generation_request."""
    return validate_generation_request(**kwargs)


@dataclass(frozen=True)
class ImageRemediationMask:
    """A server-rasterized OpenAI edit mask for one immutable plan item."""

    data_uri: str
    mode: str
    bbox: tuple[int, int, int, int] | None
    width: int
    height: int
    mask_hash: str
    source_content_hash: str
    remediation_id: int
    plan_item_id: str
    finding_ids: list[int]
    protected_fraction: float
    editable_fraction: float

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "_edit_mask_mode": self.mode,
            "_edit_mask_bbox": list(self.bbox) if self.bbox else None,
            "_edit_mask_source": "reproduction_remediation",
            "_reproduction_remediation_id": self.remediation_id,
            "_reproduction_plan_item_id": self.plan_item_id,
            "_reproduction_mask_source_content_hash": self.source_content_hash,
            "_reproduction_mask_hash": self.mask_hash,
            "_reproduction_mask_finding_ids": list(self.finding_ids),
            "_reproduction_mask_protected_fraction": round(self.protected_fraction, 6),
            "_reproduction_mask_editable_fraction": round(self.editable_fraction, 6),
        }


def _decode_data_uri_image(value: str) -> Image.Image:
    try:
        header, encoded = str(value).split(",", 1)
        if not header.lower().startswith("data:image/") or ";base64" not in header.lower():
            raise ValueError("not an image data URI")
        raw = base64.b64decode(encoded, validate=True)
        image = Image.open(io.BytesIO(raw))
        image.load()
        if image.width <= 0 or image.height <= 0:
            raise ValueError("empty image")
        return image
    except Exception as exc:  # noqa: BLE001 - normalize gateway image parsing failures
        raise ReproductionRemediationError("编辑源图无法用于纠偏蒙版栅格化") from exc


def _normalized_bbox(value: dict[str, Any]) -> tuple[float, float, float, float]:
    for x_key, y_key, width_key, height_key in (
        ("x", "y", "width", "height"),
        ("left", "top", "width", "height"),
        ("x", "y", "w", "h"),
    ):
        try:
            coords = tuple(float(value[key]) for key in (x_key, y_key, width_key, height_key))
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(number) for number in coords) and _valid_bbox(value):
            return coords
    raise ReproductionRemediationError("纠偏 finding 缺少有效的归一化 bbox")


def _target_asset_content_hash(db: Session, target: ResolvedAsset) -> str:
    if target.origin != "generated" or not isinstance(target.row, GenAsset):
        raise ReproductionRemediationConflict("图片纠偏目标必须是受控生成资产")
    url = target.row.hd_url if target.row.unlocked and target.row.hd_url else target.row.preview_url
    key = storage.key_from_url(str(url or ""))
    if not key:
        raise ReproductionRemediationConflict("图片纠偏目标缺少可验证的受控素材")
    try:
        # The image gateway reads this exact owner-gated path and prefers its
        # clean model_ref sidecar over a watermarked preview.  Comparing the
        # preview hash here would reject a legitimate inpaint request whenever
        # the two bytes differ.
        path = asset_refs.generated_asset_reference_path(
            db,
            int(target.row.user_id),
            key,
        )
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception as exc:  # normalize owner/storage/reference failures
        raise ReproductionRemediationConflict("图片纠偏目标文件不可用") from exc
    if not digest:
        raise ReproductionRemediationConflict("图片纠偏目标文件不可用")
    return str(digest)


def rasterize_image_remediation_mask(
    db: Session,
    *,
    task: GenTask,
    reference_data_uri: str,
    reference_content_hash: str | None,
) -> ImageRemediationMask:
    """Rebuild a mask from persisted finding boxes, never from client input.

    OpenAI image edit semantics are transparent = editable, opaque = protected.
    Starting opaque therefore protects every unselected pixel, then selected
    finding boxes are opened as the only editable regions.
    """
    context = (task.params or {}).get("_reproduction_context")
    if not isinstance(context, dict):
        raise ReproductionRemediationConflict("生成任务没有纠偏计划上下文")
    remediation_id = int(context.get("remediation_id") or 0)
    plan_item_id = str(context.get("plan_item_id") or "")
    if remediation_id <= 0 or not plan_item_id:
        raise ReproductionRemediationConflict("生成任务纠偏计划上下文无效")
    row = _owned_remediation(db, remediation_id=remediation_id, user_id=int(task.user_id))
    if row.mode != "image_inpaint" or task.category != "image":
        raise ReproductionRemediationConflict("生成任务与图片局部纠偏计划不匹配")
    snapshot = deepcopy(row.plan_snapshot or [])
    if _hash(snapshot) != str(row.plan_hash) or str(context.get("plan_hash") or "") != row.plan_hash:
        raise ReproductionRemediationConflict("图片纠偏计划快照校验失败")
    item = next((value for value in snapshot if value.get("item_id") == plan_item_id), None)
    if item is None or item.get("kind") != "image_inpaint":
        raise ReproductionRemediationNotFound("图片纠偏计划项不存在")
    target = _resolve_asset(db, user_id=int(task.user_id), asset_ref=row.target_asset_ref)
    expected_url = _asset_url(target)
    if str(task.source_asset_url or "") != expected_url:
        raise ReproductionRemediationConflict("生成任务编辑源图偏离纠偏计划目标资产")
    actual_hash = _target_asset_content_hash(db, target)
    supplied_hash = str(reference_content_hash or "").strip().lower()
    if len(supplied_hash) != 64 or any(char not in "0123456789abcdef" for char in supplied_hash):
        raise ReproductionRemediationError("编辑源图缺少有效 SHA-256 内容指纹")
    if supplied_hash != actual_hash:
        raise ReproductionRemediationConflict("编辑源图内容已变化，不能复用纠偏蒙版")
    # Decoding verifies the gateway reference itself is a real image.  The
    # content hash intentionally identifies the original controlled asset,
    # because gateway preparation may resize it before transport.
    reference = _decode_data_uri_image(reference_data_uri)
    finding_ids = [int(value) for value in item.get("finding_ids") or []]
    findings = list(db.scalars(select(ReproductionFinding).where(
        ReproductionFinding.assessment_id == int(row.assessment_id),
        ReproductionFinding.id.in_(finding_ids),
    )).all())
    if {int(finding.id) for finding in findings} != set(finding_ids):
        raise ReproductionRemediationConflict("图片纠偏 finding 快照已损坏")
    alpha = Image.new("L", reference.size, 255)
    draw = ImageDraw.Draw(alpha)
    for finding in findings:
        x, y, width, height = _normalized_bbox(finding.bbox or {})
        left = max(0, min(reference.width - 1, math.floor(x * reference.width)))
        top = max(0, min(reference.height - 1, math.floor(y * reference.height)))
        right = max(left, min(reference.width - 1, math.ceil((x + width) * reference.width) - 1))
        bottom = max(top, min(reference.height - 1, math.ceil((y + height) * reference.height) - 1))
        draw.rectangle((left, top, right, bottom), fill=0)
    protected_bbox = alpha.getbbox()
    bbox = (
        (protected_bbox[0], protected_bbox[1], protected_bbox[2] - 1, protected_bbox[3] - 1)
        if protected_bbox else None
    )
    mask = Image.new("RGBA", reference.size, (255, 255, 255, 0))
    mask.putalpha(alpha)
    output = io.BytesIO()
    mask.save(output, format="PNG", optimize=False)
    raw = output.getvalue()
    protected_pixels = sum(1 for value in alpha.tobytes() if value > 0)
    total_pixels = max(1, reference.width * reference.height)
    return ImageRemediationMask(
        data_uri=f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}",
        mode="reproduction_remediation",
        bbox=bbox,
        width=reference.width,
        height=reference.height,
        mask_hash=hashlib.sha256(raw).hexdigest(),
        source_content_hash=actual_hash,
        remediation_id=int(row.id),
        plan_item_id=plan_item_id,
        finding_ids=finding_ids,
        protected_fraction=protected_pixels / total_pixels,
        editable_fraction=(total_pixels - protected_pixels) / total_pixels,
    )


def attach_image_remediation_mask_metadata(
    db: Session, *, task: GenTask, mask: ImageRemediationMask, commit: bool = False
) -> None:
    """Persist mask provenance beside the generated reverse revision."""
    if task.generation_revision_id is None:
        return
    revision = db.get(ReverseResultRevision, int(task.generation_revision_id))
    if revision is None or int(revision.user_id) != int(task.user_id):
        raise ReproductionRemediationConflict("生成版本与纠偏蒙版血缘不一致")
    payload = deepcopy(revision.payload) if isinstance(revision.payload, dict) else {}
    payload["image_mask"] = {
        "schema_version": "reproduction-remediation-mask.v1",
        "remediation_id": mask.remediation_id,
        "plan_item_id": mask.plan_item_id,
        "finding_ids": list(mask.finding_ids),
        "source_content_hash": mask.source_content_hash,
        "mask_hash": mask.mask_hash,
        "width": mask.width,
        "height": mask.height,
        "protected_fraction": round(mask.protected_fraction, 6),
        "editable_fraction": round(mask.editable_fraction, 6),
    }
    revision.payload = payload
    revision.payload_hash = reverse_lineage.canonical_payload_hash(payload)
    db.flush()
    if commit:
        db.commit()


def bind_generation_task(
    db: Session,
    *,
    user_id: int,
    remediation_id: int,
    plan_item_id: str,
    task_id: int,
    commit: bool = False,
) -> ReproductionRemediation:
    row = _owned_remediation(db, remediation_id=remediation_id, user_id=user_id)
    snapshot = deepcopy(row.plan_snapshot or [])
    if _hash(snapshot) != str(row.plan_hash):
        raise ReproductionRemediationConflict("纠偏计划快照校验失败")
    index = next((i for i, value in enumerate(snapshot) if value.get("item_id") == plan_item_id), None)
    if index is None:
        raise ReproductionRemediationNotFound("纠偏计划项不存在")
    task = db.get(GenTask, int(task_id))
    if task is None or int(task.user_id) != int(user_id):
        raise ReproductionRemediationNotFound("生成任务不存在")
    expected_category = "image" if row.mode == "image_inpaint" else "video"
    if task.category != expected_category:
        raise ReproductionRemediationConflict("生成任务类型与纠偏计划不匹配")
    expected = snapshot[index].get("request")
    if not isinstance(expected, dict) or task.client_request_id != expected.get("client_request_id"):
        raise ReproductionRemediationConflict("生成任务不属于纠偏计划项")
    context = (task.params or {}).get("_reproduction_context")
    if not isinstance(context, dict) or (
        int(context.get("remediation_id") or 0) != int(row.id)
        or str(context.get("plan_item_id") or "") != str(plan_item_id)
        or str(context.get("plan_hash") or "") != str(row.plan_hash)
    ):
        raise ReproductionRemediationConflict("生成任务缺少可信的纠偏计划上下文")
    task_ids = [int(value) for value in row.generation_task_ids or []]
    if index < len(task_ids):
        if task_ids[index] != int(task_id):
            raise ReproductionRemediationConflict("纠偏计划项已绑定其他生成任务")
        return row
    if index != len(task_ids):
        raise ReproductionRemediationConflict("纠偏计划项必须按计划顺序绑定生成任务")
    if int(task_id) in task_ids:
        raise ReproductionRemediationConflict("同一生成任务不能绑定多个纠偏计划项")
    task_ids.append(int(task_id))
    row.generation_task_ids = task_ids
    if row.status == "planned":
        row.status = "generating"
        row.started_at = row.started_at or _utcnow()
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    return row


def _workflow_composition_config(row: ReproductionRemediation) -> dict[str, Any]:
    """Keep user controls cosmetic; asset refs and timeline stay server-owned."""
    raw = row.video_composition if isinstance(row.video_composition, dict) else {}
    result: dict[str, Any] = {}
    for key in ("title", "canvas", "subtitles", "audio_tracks", "original_audio_volume"):
        value = raw.get(key)
        if value is not None:
            result[key] = deepcopy(value)
    transitions = raw.get("transitions")
    if isinstance(transitions, dict):
        result["transitions"] = deepcopy(transitions)
    return result


def _video_composition_payload(
    db: Session,
    *,
    row: ReproductionRemediation,
    tasks: list[GenTask],
) -> dict[str, Any]:
    revision = db.get(ReverseResultRevision, int(row.applied_revision_id))
    if revision is None or int(revision.user_id) != int(row.user_id):
        raise ReproductionRemediationConflict("已应用纠偏版本不存在")
    try:
        _payload, timeline = reverse_operations._revision_shots(revision)
    except reverse_operations.ReverseOperationInvalid as exc:
        raise ReproductionRemediationConflict("已应用纠偏版本缺少完整视频分镜") from exc
    plan_by_shot = {
        str(item.get("shot_id")): (index, item)
        for index, item in enumerate(row.plan_snapshot or [])
        if item.get("shot_id") is not None
    }
    if set(plan_by_shot) != {str(value) for value in row.selected_shot_ids or []}:
        raise ReproductionRemediationConflict("视频纠偏计划镜头快照已损坏")
    target = _resolve_asset(db, user_id=int(row.user_id), asset_ref=row.target_asset_ref)
    if _asset_media_type(target) != "video":
        raise ReproductionRemediationConflict("视频纠偏目标资产不可用")
    assets = {
        int(asset.task_id): asset
        for asset in db.scalars(
            select(GenAsset).where(
                GenAsset.task_id.in_([int(task.id) for task in tasks]),
                GenAsset.user_id == int(row.user_id),
                GenAsset.type == "video",
            )
        ).all()
        if asset.task_id is not None
    }
    config = _workflow_composition_config(row)
    transitions = config.pop("transitions", {})
    shots: list[dict[str, Any]] = []
    for raw_shot in timeline:
        shot_id = str(raw_shot.get("shot_id") or "").strip()
        if not shot_id:
            raise ReproductionRemediationConflict("已应用纠偏版本包含无效 shot_id")
        try:
            start = float(raw_shot["start_seconds"])
            end = float(raw_shot["end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReproductionRemediationConflict("已应用纠偏版本包含无效镜头时间范围") from exc
        item_pair = plan_by_shot.get(shot_id)
        if item_pair is None:
            asset_ref = row.target_asset_ref
            generation_task_id = None
            source_range = {
                "source_start_seconds": start,
                "source_end_seconds": end,
            }
        else:
            index, _item = item_pair
            if index >= len(tasks) or tasks[index].status != "succeeded":
                raise ReproductionRemediationConflict("选中镜头尚未生成成功")
            asset = assets.get(int(tasks[index].id))
            if asset is None:
                raise ReproductionRemediationConflict("选中镜头生成任务缺少视频资产")
            asset_ref = f"g.{int(asset.id)}"
            generation_task_id = int(tasks[index].id)
            # A regenerated shot is a new standalone clip. Applying the old
            # absolute source window would seek past its duration.
            source_range = {}
        transition = transitions.get(shot_id) if isinstance(transitions, dict) else None
        shots.append(
            {
                "shot_id": shot_id,
                "asset_ref": asset_ref,
                "generation_task_id": generation_task_id,
                **source_range,
                **({"transition": deepcopy(transition)} if isinstance(transition, dict) else {}),
            }
        )
    if not shots:
        raise ReproductionRemediationConflict("已应用纠偏版本没有可合成镜头")
    return {
        "schema_version": "video-composition.v1",
        "reverse_operation_id": int(revision.operation_id),
        "shots": shots,
        **config,
    }


def _start_video_composition(
    db: Session, *, row: ReproductionRemediation, tasks: list[GenTask]
) -> None:
    if row.composition_workflow_run_id is not None:
        return
    from . import generation_quotes, tool_workflows

    payload = _video_composition_payload(db, row=row, tasks=tasks)
    request_id = f"remediation-compose-{int(row.id)}-{row.plan_hash[:16]}"
    draft = ToolRunCreateIn(
        tool_slug="storyboard-compose",
        client_request_id=request_id,
        input={"composition": payload},
    )
    quote = generation_quotes.create_workflow_quote(db, user_id=int(row.user_id), body=draft)
    # The storyboard tool is deliberately configured with a zero-credit policy.
    if int(quote.estimated_credits or 0) != 0:
        raise ReproductionRemediationConflict("storyboard-compose 必须使用零积分内部工作流")
    run, _created = tool_workflows.create_run(
        db,
        user_id=int(row.user_id),
        body=draft.model_copy(update={"quote_id": int(quote.id)}),
    )
    row.composition_workflow_run_id = int(run.id)
    row.composition_tool_run_id = int(run.tool_run_id)
    row.status = "composing"
    row.started_at = row.started_at or _utcnow()
    row.error_code = None
    row.error = None
    try:
        tool_workflows.enqueue_run(int(run.id))
    except Exception:  # noqa: BLE001 - durable workflow dispatch outbox retries publication
        log.exception("remediation composition workflow enqueue failed run_id=%s", run.id)


def _composition_final_asset(
    db: Session, *, row: ReproductionRemediation
) -> GenAsset | None:
    if row.composition_tool_run_id is None:
        return None
    task = db.scalar(
        select(GenTask)
        .where(
            GenTask.user_id == int(row.user_id),
            GenTask.client_request_id == f"workflow-compose-{int(row.composition_tool_run_id)}-compose",
            GenTask.status == "succeeded",
        )
        .order_by(GenTask.id.desc())
        .limit(1)
    )
    if task is None:
        return None
    return db.scalar(
        select(GenAsset)
        .where(
            GenAsset.task_id == int(task.id),
            GenAsset.user_id == int(row.user_id),
            GenAsset.type == "video",
        )
        .order_by(GenAsset.id.desc())
        .limit(1)
    )


def _start_successor_assessment(db: Session, *, row: ReproductionRemediation) -> None:
    """Start assessment only after a remediation has one final media asset."""
    if not row.auto_reassess or row.successor_assessment_id is not None or row.final_asset_id is None:
        return
    final_asset = db.get(GenAsset, int(row.final_asset_id))
    expected_type = "image" if row.mode == "image_inpaint" else "video"
    if final_asset is None or final_asset.type != expected_type or final_asset.task_id is None:
        raise ReproductionRemediationConflict("最终纠偏资产不存在或媒体类型不匹配")
    body = reproduction_assessment.ReproductionAssessmentCreateIn(
        source_asset_ref=row.source_asset_ref,
        generated_asset_ref=f"g.{int(final_asset.id)}",
        generation_task_id=int(final_asset.task_id),
        idempotency_key=f"remediation-reassess-{int(row.id)}-{int(final_asset.id)}",
    )
    assert body.source_asset_ref == row.source_asset_ref
    assert body.generated_asset_ref == f"g.{int(final_asset.id)}"
    assessment, created = reproduction_assessment.create_assessment(
        db, user_id=int(row.user_id), body=body
    )
    row.successor_assessment_id = int(assessment.id)
    if created:
        try:
            reproduction_assessment.enqueue_assessment(int(assessment.id))
        except Exception as exc:  # creation is durable; retain an observable terminal result
            reproduction_assessment.mark_enqueue_failed(db, assessment=assessment, exc=exc)
    row.status = "reassessing"


def _reconcile_successor_assessment(db: Session, *, row: ReproductionRemediation) -> bool:
    """Project the final remediation assessment back onto remediation state."""
    if row.successor_assessment_id is None:
        return False
    assessment = db.get(ReproductionAssessment, int(row.successor_assessment_id))
    if assessment is None or int(assessment.user_id) != int(row.user_id):
        row.status = "failed"
        row.error_code = "REMEDIATION_SUCCESSOR_ASSESSMENT_INTEGRITY"
        row.error = "后继复刻度评估不存在或不属于当前用户"
        row.finished_at = _utcnow()
        return True
    if assessment.status in {"queued", "running"}:
        row.status = "reassessing"
        return True
    if assessment.status == "succeeded":
        row.status = "succeeded"
        row.error_code = None
        row.error = None
    elif assessment.status == "partial":
        row.status = "partial"
        row.error_code = "REMEDIATION_REASSESSMENT_PARTIAL"
        row.error = "最终纠偏资产复刻度评估仅部分完成"
    else:
        row.status = "failed"
        row.error_code = "REMEDIATION_REASSESSMENT_FAILED"
        row.error = str(assessment.error or "最终纠偏资产复刻度评估失败")[:2000]
    row.finished_at = _utcnow()
    return True


def reconcile_remediation(
    db: Session, *, remediation_id: int, commit: bool = False
) -> ReproductionRemediation:
    row = db.get(ReproductionRemediation, int(remediation_id))
    if row is None:
        raise ReproductionRemediationNotFound("纠偏执行计划不存在")
    if row.status in {"canceled", "failed", "partial", "succeeded"}:
        return row
    if _reconcile_successor_assessment(db, row=row):
        db.flush()
        if commit:
            db.commit()
            db.refresh(row)
        return row
    task_ids = [int(value) for value in row.generation_task_ids or []]
    if not task_ids and row.composition_workflow_run_id is None:
        return row
    tasks = {
        int(task.id): task
        for task in db.scalars(select(GenTask).where(GenTask.id.in_(task_ids))).all()
    }
    observed = [tasks.get(task_id) for task_id in task_ids]
    if any(task is None or int(task.user_id) != int(row.user_id) for task in observed):
        row.status = "failed"
        row.error_code = "REMEDIATION_TASK_INTEGRITY"
        row.error = "纠偏计划绑定的生成任务不存在或不属于当前用户"
    elif len(task_ids) < len(row.plan_snapshot or []) or any(task.status in _ACTIVE_TASK_STATUSES for task in observed):
        row.status = "generating"
        row.started_at = row.started_at or _utcnow()
    elif observed and all(task.status == "succeeded" for task in observed):
        if row.mode == "image_inpaint":
            assets = list(
                db.scalars(
                    select(GenAsset)
                    .where(
                        GenAsset.task_id.in_(task_ids),
                        GenAsset.user_id == int(row.user_id),
                    )
                    .order_by(GenAsset.id.desc())
                ).all()
            )
            if not assets:
                row.status = "failed"
                row.error_code = "REMEDIATION_FINAL_ASSET_MISSING"
                row.error = "图片纠偏生成任务缺少结果资产"
                row.finished_at = _utcnow()
            else:
                row.final_asset_id = int(assets[0].id)
                _start_successor_assessment(db, row=row)
                if not row.auto_reassess:
                    row.status = "succeeded"
                    row.finished_at = _utcnow()
                row.error_code = None
                row.error = None
        elif row.composition_workflow_run_id is None:
            try:
                _start_video_composition(db, row=row, tasks=[task for task in observed if task is not None])
            except Exception as exc:  # workflow/tool catalog faults must become durable plan failures
                row.status = "failed"
                row.error_code = getattr(exc, "code", "REMEDIATION_COMPOSITION_FAILED")
                row.error = str(exc)
                row.finished_at = _utcnow()
        else:
            workflow = db.get(WorkflowRun, int(row.composition_workflow_run_id))
            if workflow is None or int(workflow.user_id) != int(row.user_id):
                row.status = "failed"
                row.error_code = "REMEDIATION_COMPOSITION_INTEGRITY"
                row.error = "视频合成工作流不存在或不属于当前用户"
                row.finished_at = _utcnow()
            elif workflow.status in {"queued", "running", "waiting_review", "compensating"}:
                row.status = "composing"
            elif workflow.status == "succeeded":
                final_asset = _composition_final_asset(db, row=row)
                if final_asset is None:
                    row.status = "failed"
                    row.error_code = "REMEDIATION_COMPOSITION_ASSET_MISSING"
                    row.error = "视频合成工作流完成但缺少最终视频资产"
                    row.finished_at = _utcnow()
                else:
                    row.final_asset_id = int(final_asset.id)
                    _start_successor_assessment(db, row=row)
                    if not row.auto_reassess:
                        row.status = "succeeded"
                        row.finished_at = _utcnow()
                    row.error_code = None
                    row.error = None
            else:
                row.status = "failed"
                row.error_code = "REMEDIATION_COMPOSITION_FAILED"
                row.error = str(workflow.error or "视频合成工作流失败")[:2000]
                row.finished_at = _utcnow()
    elif all(task.status in _TERMINAL_TASK_STATUSES for task in observed):
        succeeded = any(task.status == "succeeded" for task in observed)
        row.status = "partial" if succeeded else "failed"
        row.finished_at = _utcnow()
        row.error_code = "REMEDIATION_GENERATION_PARTIAL" if succeeded else "REMEDIATION_GENERATION_FAILED"
        row.error = "部分纠偏生成任务未成功" if succeeded else "纠偏生成任务未成功"
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    return row


def reconcile_all_remediations(
    db: Session | None = None, *, limit: int = 200, commit: bool = False
) -> dict[str, int]:
    owned_session = db is None
    if db is None:
        from ..db import SessionLocal

        db = SessionLocal()
        commit = True
    assert db is not None
    try:
        rows = list(
            db.scalars(
                select(ReproductionRemediation)
                .where(
                    ReproductionRemediation.status.in_(
                        ("planned", "generating", "composing", "reassessing")
                    )
                )
                .order_by(
                    ReproductionRemediation.updated_at.asc(),
                    ReproductionRemediation.id.asc(),
                )
                .limit(max(1, min(int(limit), 500)))
            ).all()
        )
        changed = 0
        for row in rows:
            before = row.status
            reconcile_remediation(db, remediation_id=int(row.id), commit=False)
            changed += int(before != row.status)
        if commit:
            db.commit()
        return {"checked": len(rows), "changed": changed}
    except Exception:
        if commit:
            db.rollback()
        raise
    finally:
        if owned_session:
            db.close()


def reconcile_for_external(external_kind: str, external_id: int) -> None:
    """Best-effort state projection for task/workflow/assessment callbacks."""
    kind = str(external_kind or "").strip()
    try:
        external_id = int(external_id)
    except (TypeError, ValueError):
        return
    if external_id <= 0:
        return
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        if kind == "generation_task":
            rows = list(
                db.scalars(
                    select(ReproductionRemediation).where(
                        ReproductionRemediation.status.in_(
                            ("planned", "generating", "composing", "reassessing")
                        )
                    )
                ).all()
            )
            rows = [row for row in rows if external_id in {int(value) for value in row.generation_task_ids or []}]
        elif kind == "workflow_run":
            rows = list(
                db.scalars(
                    select(ReproductionRemediation).where(
                        ReproductionRemediation.composition_workflow_run_id == external_id
                    )
                ).all()
            )
        elif kind == "reproduction_assessment":
            rows = list(
                db.scalars(
                    select(ReproductionRemediation).where(
                        ReproductionRemediation.successor_assessment_id == external_id
                    )
                ).all()
            )
        else:
            return
        for row in rows:
            reconcile_remediation(db, remediation_id=int(row.id), commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
