"""Shared response builders for generation tasks."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GatewayCall, GenAsset, GenTask
from ..schemas import TaskOut
from .asset_output import to_asset_out
from .error_codes import task_error_type
from .generation import is_terminal_status
from .generation_pricing import generation_cost_from_snapshot
from .progress import get_progress
from .task_eta import video_eta_for_task

_PUBLIC_PARAM_KEYS = {
    "n",
    "size",
    "duration",
    "target_duration",
    "resolution",
    "target_resolution",
    "ratio",
    "reference_width",
    "reference_height",
    "subject_mode",
}


def _public_params(task: GenTask) -> dict:
    params = task.params or {}
    return {k: v for k, v in params.items() if k in _PUBLIC_PARAM_KEYS}


def _task_out_base(task: GenTask) -> TaskOut:
    original_params = task.params
    if original_params is not None:
        return TaskOut.model_validate(task)
    task.params = {}
    try:
        return TaskOut.model_validate(task)
    finally:
        task.params = original_params


def _progress_for(task: GenTask) -> int:
    # Terminal tasks are always 100, so no Redis lookup is needed.
    if is_terminal_status(task.status):
        return 100
    return get_progress(task.id)["percent"]


def _decorate_partial(out: TaskOut, task: GenTask) -> None:
    params = task.params or {}
    if not params.get("_partial"):
        return
    out.partial = True
    out.requested_count = params.get("_requested_n")
    out.saved_count = params.get("_saved_n")
    out.skipped_count = params.get("_skipped_n")
    errors = params.get("_partial_errors")
    if isinstance(errors, list):
        out.partial_errors = [str(e)[:160] for e in errors[:5] if str(e).strip()]


def _decorate_error(out: TaskOut, task: GenTask) -> None:
    if not task.error and task.status not in {"failed", "needs_review"}:
        return
    out.error_message = task.error
    out.error_type = task_error_type(task.params, task.error, status=task.status)


def _decorate_generation_details(
    out: TaskOut,
    task: GenTask,
    *,
    gateway_model_id: str | None = None,
) -> None:
    params = task.params or {}
    snapshot = params.get("_model_snapshot") if isinstance(params.get("_model_snapshot"), dict) else {}
    prompt_obj = task.prompt if isinstance(task.prompt, dict) else {}
    generation_prompt = str(params.get("_generation_prompt") or "").strip()
    request_prompt = str(prompt_obj.get("final_text") or prompt_obj.get("instruction") or "").strip()
    raw_prompt = str(prompt_obj.get("raw_text") or "").strip()
    optimized_prompt = str(prompt_obj.get("optimized_text") or "").strip()
    has_layered_prompt = bool(
        raw_prompt
        or optimized_prompt
        or prompt_obj.get("optimizer_model_id")
        or prompt_obj.get("prompt_optimizer_model_id")
    )
    assembled_prompt = str(
        prompt_obj.get("assembled_text") or (request_prompt if has_layered_prompt else "")
    ).strip()
    out.request_prompt_text = request_prompt or None
    out.raw_prompt_text = raw_prompt or None
    out.optimized_prompt_text = optimized_prompt or None
    out.assembled_prompt_text = assembled_prompt or None
    out.generation_prompt_text = generation_prompt or None
    out.prompt_text = generation_prompt or request_prompt or None
    out.prompt_text_source = "generation" if generation_prompt else ("request" if request_prompt else None)
    out.model_name = str(snapshot.get("model_name") or "").strip() or None
    out.model_id = str(snapshot.get("model_id") or gateway_model_id or "").strip() or None
    out.model_provider = str(snapshot.get("provider") or "").strip() or None
    out.prompt_optimizer_model_id = str(
        prompt_obj.get("prompt_optimizer_model_id")
        or prompt_obj.get("optimizer_model_id")
        or params.get("_prompt_optimizer_model_id")
        or ""
    ).strip() or None
    out.prompt_compiler_version = str(params.get("_prompt_compiler_version") or "").strip() or None
    warnings = params.get("_video_prompt_warnings")
    out.prompt_warnings = [str(item).strip() for item in warnings if str(item).strip()] \
        if isinstance(warnings, list) else []
    overlays = params.get("_post_overlays")
    out.post_overlays = [str(item).strip() for item in overlays if str(item).strip()] \
        if isinstance(overlays, list) else []
    out.voiceover = str(params.get("_voiceover") or "").strip() or None
    sfx = params.get("_sfx")
    out.sfx = [str(item).strip() for item in sfx if str(item).strip()] \
        if isinstance(sfx, list) else []
    out.sequence_required = bool(params.get("_video_prompt_sequence_required"))


def _gateway_model_for_task(db: Session, task_id: int) -> str | None:
    return db.execute(
        select(GatewayCall.model_id)
        .where(GatewayCall.task_id == task_id, GatewayCall.model_id.is_not(None))
        .order_by(GatewayCall.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _decorate_eta(db: Session, out: TaskOut, task: GenTask) -> None:
    eta = video_eta_for_task(db, task)
    if not eta:
        return
    out.eta_source = eta["eta_source"]
    out.eta_total_seconds = eta["eta_total_seconds"]
    out.eta_remaining_seconds = eta["eta_remaining_seconds"]
    out.eta_sample_count = eta["eta_sample_count"]
    if not out.progress or out.progress <= 8:
        out.progress = max(out.progress or 0, int(eta.get("progress_estimate") or 0))


def _set_final_summary(out: TaskOut, final_task: GenTask | None, asset_count: int = 0) -> None:
    if not final_task:
        return
    out.final_task_id = final_task.id
    out.final_status = final_task.status
    out.final_asset_count = int(asset_count or 0)


def _set_final_cost_estimate(out: TaskOut, task: GenTask) -> None:
    if task.category != "video" or task.stage != "preview":
        return
    snapshot = (task.params or {}).get("_model_snapshot") or {}
    out.final_cost_estimate = generation_cost_from_snapshot(
        snapshot,
        category="video",
        stage="final",
        params=task.params or {},
        n=1,
        source_type=task.source_type,
    )


def _prefer_final_task(current: GenTask | None, candidate: GenTask) -> GenTask:
    preferred_statuses = {"queued", "running", "needs_review", "succeeded"}
    if current is None:
        return candidate
    current_preferred = current.status in preferred_statuses
    candidate_preferred = candidate.status in preferred_statuses
    if candidate_preferred and not current_preferred:
        return candidate
    if candidate_preferred == current_preferred and candidate.id > current.id:
        return candidate
    return current


def build_task_out(db: Session, task: GenTask) -> TaskOut:
    assets = list(
        db.execute(
            select(GenAsset).where(GenAsset.task_id == task.id).order_by(GenAsset.id)
        ).scalars()
    )
    out = _task_out_base(task)
    out.params = _public_params(task)
    out.assets = [to_asset_out(db, a, task=task) for a in assets]
    out.progress = _progress_for(task)
    _decorate_partial(out, task)
    _decorate_error(out, task)
    _decorate_generation_details(out, task, gateway_model_id=_gateway_model_for_task(db, task.id))
    _decorate_eta(db, out, task)
    _set_final_cost_estimate(out, task)
    if task.category == "video" and task.stage == "preview":
        final_tasks = list(db.execute(
            select(GenTask)
            .where(
                GenTask.user_id == task.user_id,
                GenTask.category == "video",
                GenTask.stage == "final",
                GenTask.parent_task_id == task.id,
            )
            .order_by(GenTask.id.desc())
        ).scalars())
        final_task = None
        for ft in final_tasks:
            final_task = _prefer_final_task(final_task, ft)
        if final_task:
            _set_final_summary(
                out,
                final_task,
                len(db.execute(
                    select(GenAsset.id).where(GenAsset.task_id == final_task.id)
                ).all()),
            )
    return out


def build_task_outs(db: Session, tasks: list[GenTask]) -> list[TaskOut]:
    """Build a task page with one asset query for the whole result set."""
    ids = [t.id for t in tasks]
    by_task: dict[int, list[GenAsset]] = {}
    if ids:
        rows = db.execute(
            select(GenAsset).where(GenAsset.task_id.in_(ids)).order_by(GenAsset.id)
        ).scalars()
        for a in rows:
            by_task.setdefault(a.task_id, []).append(a)
    gateway_model_by_task: dict[int, str] = {}
    if ids:
        gateway_rows = db.execute(
            select(GatewayCall.task_id, GatewayCall.model_id)
            .where(GatewayCall.task_id.in_(ids), GatewayCall.model_id.is_not(None))
            .order_by(GatewayCall.id.desc())
        ).all()
        for task_id, model_id in gateway_rows:
            if task_id is not None and model_id:
                gateway_model_by_task.setdefault(int(task_id), str(model_id))
    preview_ids = [
        t.id
        for t in tasks
        if t.category == "video" and t.stage == "preview"
    ]
    final_by_parent: dict[int, GenTask] = {}
    final_asset_counts: dict[int, int] = {}
    if preview_ids:
        final_rows = list(
            db.execute(
                select(GenTask)
                .where(
                    GenTask.category == "video",
                    GenTask.stage == "final",
                    GenTask.parent_task_id.in_(preview_ids),
                )
                .order_by(GenTask.parent_task_id, GenTask.id.desc())
            ).scalars()
        )
        final_ids = []
        for ft in final_rows:
            current = final_by_parent.get(ft.parent_task_id)
            chosen = _prefer_final_task(current, ft)
            if chosen is not current:
                if current and current.id in final_ids:
                    final_ids.remove(current.id)
                final_by_parent[ft.parent_task_id] = chosen
                final_ids.append(chosen.id)
        if final_ids:
            count_rows = db.execute(
                select(GenAsset.task_id, GenAsset.id).where(GenAsset.task_id.in_(final_ids))
            ).all()
            for task_id, _asset_id in count_rows:
                final_asset_counts[task_id] = final_asset_counts.get(task_id, 0) + 1
    outs: list[TaskOut] = []
    for t in tasks:
        out = _task_out_base(t)
        out.params = _public_params(t)
        out.assets = [to_asset_out(db, a, task=t) for a in by_task.get(t.id, [])]
        out.progress = _progress_for(t)
        _decorate_partial(out, t)
        _decorate_error(out, t)
        _decorate_generation_details(out, t, gateway_model_id=gateway_model_by_task.get(t.id))
        _decorate_eta(db, out, t)
        _set_final_cost_estimate(out, t)
        _set_final_summary(
            out,
            final_by_parent.get(t.id),
            final_asset_counts.get(final_by_parent[t.id].id, 0) if t.id in final_by_parent else 0,
        )
        outs.append(out)
    return outs
