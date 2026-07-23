"""Generation task submission side effects."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    GenerationQuote,
    GenTask,
    ReverseOperation,
    ReverseResultRevision,
)
from ..schemas import TaskOut
from . import audit, credits, project_collection, recipe_usage, reverse_lineage
from .generation_quotes import consume_generation_quote
from .prompt_history import remember_prompt
from .task_output import build_task_out

_GENERATION_SOURCE_REVISION_TYPES = frozenset({"applied"})


def _lineage_output_purposes(
    operation: ReverseOperation,
    revision: ReverseResultRevision,
) -> set[str]:
    context = operation.request_context if isinstance(operation.request_context, dict) else {}
    payload = revision.payload if isinstance(revision.payload, dict) else {}
    return {
        str(value).strip()
        for value in (
            getattr(operation, "output_purpose", None),
            context.get("output_purpose"),
            payload.get("output_purpose"),
        )
        if value is not None and str(value).strip()
    }


def validate_generation_lineage(
    db: Session,
    *,
    user_id: int,
    reverse_operation_id: int | None,
    reverse_revision_id: int | None,
    for_update: bool = False,
) -> tuple[ReverseOperation | None, ReverseResultRevision | None]:
    """Resolve an owned, successful reverse result that may feed generation."""
    if reverse_operation_id is None and reverse_revision_id is None:
        return None, None
    if reverse_operation_id is None or reverse_revision_id is None:
        raise HTTPException(400, "反推任务和结果版本必须同时提供")

    operation_stmt = select(ReverseOperation).where(
        ReverseOperation.id == reverse_operation_id,
        ReverseOperation.user_id == user_id,
    )
    if for_update:
        operation_stmt = operation_stmt.with_for_update()
    operation = db.scalar(operation_stmt)
    if operation is None:
        raise HTTPException(404, "反推任务不存在")
    if operation.status != "succeeded":
        raise HTTPException(409, "只有已成功的反推任务可以用于生成")

    revision_stmt = select(ReverseResultRevision).where(
        ReverseResultRevision.id == reverse_revision_id,
        ReverseResultRevision.operation_id == operation.id,
        ReverseResultRevision.user_id == user_id,
    )
    if for_update:
        revision_stmt = revision_stmt.with_for_update()
    revision = db.scalar(revision_stmt)
    if revision is None:
        raise HTTPException(404, "反推结果版本不存在")
    if "analysis_report" in _lineage_output_purposes(operation, revision):
        raise HTTPException(409, "分析报告用途的反推结果不能用于报价或生成")
    if revision.source not in _GENERATION_SOURCE_REVISION_TYPES:
        raise HTTPException(409, "生成必须使用同一反推任务中已应用的 applied 结果版本")
    try:
        reverse_lineage.validate_revision_chain(
            db,
            revision,
            terminal_source="applied",
        )
    except reverse_lineage.ReverseLineageError as exc:
        raise HTTPException(409, str(exc)) from exc
    return operation, revision


def _public_generation_params(params: dict) -> dict:
    return {str(key): value for key, value in params.items() if not str(key).startswith("_")}


def _create_generation_lineage(
    db: Session,
    *,
    task: GenTask,
    quote: GenerationQuote,
    user_id: int,
    reverse_operation_id: int | None,
    reverse_revision_id: int | None,
) -> None:
    operation, source_revision = validate_generation_lineage(
        db,
        user_id=user_id,
        reverse_operation_id=reverse_operation_id,
        reverse_revision_id=reverse_revision_id,
        for_update=True,
    )
    if operation is None or source_revision is None:
        return

    latest_version = db.scalar(
        select(func.max(ReverseResultRevision.version)).where(
            ReverseResultRevision.operation_id == operation.id
        )
    )
    compiled_version = int(latest_version or 0) + 1
    params = dict(task.params or {})
    model_snapshot = (
        dict(params.get("_model_snapshot") or {})
        if isinstance(params.get("_model_snapshot"), dict)
        else {}
    )
    compiled_prompt = str(
        params.get("_generation_prompt")
        or (task.prompt or {}).get("final_text")
        or (task.prompt or {}).get("instruction")
        or ""
    ).strip()
    quote_snapshot = {
        "quote_id": int(quote.id),
        "capability_version_id": int(quote.capability_version_id),
        "price_version_id": int(quote.price_version_id),
        "estimated_credits": int(quote.estimated_credits),
    }
    source_ref = {
        "operation_id": int(operation.id),
        "revision_id": int(source_revision.id),
        "revision_version": int(source_revision.version),
        "revision_source": source_revision.source,
    }
    compiled_payload = {
        "schema_version": "model-compiled.v1",
        "source": source_ref,
        "generation_task_id": int(task.id),
        "client_request_id": task.client_request_id,
        "category": task.category,
        "stage": task.stage,
        "target_model": {
            "model_config_id": task.model_config_id,
            "model_id": model_snapshot.get("model_id"),
            "model_name": model_snapshot.get("model_name"),
            "provider": model_snapshot.get("provider"),
        },
        "compiled_prompt": compiled_prompt,
        "prompt": dict(task.prompt or {}),
        "params": _public_generation_params(params),
        "compiler": {
            "version": params.get("_prompt_compiler_version"),
            "metadata": params.get("_video_prompt_metadata"),
            "warnings": list(params.get("_video_prompt_warnings") or []),
        },
        "quote": quote_snapshot,
        "request_fingerprint": params.get("_client_request_fingerprint"),
        "retry_of_task_id": task.retry_of_task_id,
    }
    compiled_revision = ReverseResultRevision(
        operation_id=operation.id,
        user_id=user_id,
        version=compiled_version,
        source="model_compiled",
        payload=compiled_payload,
        parent_revision_id=int(source_revision.id),
        source_content_hash=source_revision.source_content_hash,
        source_fingerprints=deepcopy(source_revision.source_fingerprints),
        payload_hash=reverse_lineage.canonical_payload_hash(compiled_payload),
        lineage_status=reverse_lineage.VERIFIED,
        evidence_review_action="not_applicable",
    )
    db.add(compiled_revision)
    db.flush()

    generation_payload = {
        "schema_version": "generation-lineage.v1",
        "source": source_ref,
        "compiled_revision_id": int(compiled_revision.id),
        "compiled_revision_version": int(compiled_revision.version),
        "generation": {
            "task_id": int(task.id),
            "retry_of_task_id": task.retry_of_task_id,
            "client_request_id": task.client_request_id,
            "category": task.category,
            "stage": task.stage,
            "model_config_id": task.model_config_id,
            "quote": quote_snapshot,
            "cost_frozen": int(task.cost_frozen or 0),
        },
    }
    generation_revision = ReverseResultRevision(
        operation_id=operation.id,
        user_id=user_id,
        version=compiled_version + 1,
        source="generation",
        payload=generation_payload,
        parent_revision_id=int(compiled_revision.id),
        source_content_hash=compiled_revision.source_content_hash,
        source_fingerprints=deepcopy(compiled_revision.source_fingerprints),
        payload_hash=reverse_lineage.canonical_payload_hash(generation_payload),
        lineage_status=reverse_lineage.VERIFIED,
        evidence_review_action="not_applicable",
    )
    db.add(generation_revision)
    db.flush()

    task.reverse_operation_id = int(operation.id)
    task.source_revision_id = int(source_revision.id)
    task.compiled_revision_id = int(compiled_revision.id)
    task.generation_revision_id = int(generation_revision.id)
    params["_reverse_lineage"] = {
        "operation_id": int(operation.id),
        "source_revision_id": int(source_revision.id),
        "compiled_revision_id": int(compiled_revision.id),
        "generation_revision_id": int(generation_revision.id),
    }
    task.params = params
    db.flush()


def submit_generation_task(
    db: Session,
    *,
    user_id: int,
    source_asset_url: str | None,
    source_type: str | None,
    category: str,
    stage: str,
    prompt: dict,
    model_use: str,
    model_config_id: int | None,
    quote: GenerationQuote,
    params: dict,
    client_request_id: str | None,
    cost: int,
    parent_task_id: int | None,
    project_id: int | None,
    reverse_operation_id: int | None,
    reverse_revision_id: int | None,
    recipe_attribution: recipe_usage.RecipeAttribution | None,
    ip: str | None,
    replay_conflicting_task: Callable[[], TaskOut | None],
    retry_of_task_id: int | None = None,
) -> TaskOut:
    from .generation_dispatch import create_dispatch_intent, validate_dispatch_request

    validate_dispatch_request(category)
    task = GenTask(
        user_id=user_id,
        source_asset_url=source_asset_url,
        source_type=source_type,
        category=category,
        stage=stage,
        prompt=prompt,
        model_use=model_use,
        model_config_id=model_config_id,
        quote_id=quote.id,
        params=params,
        client_request_id=client_request_id,
        status="queued",
        cost_frozen=cost,
        parent_task_id=parent_task_id,
        retry_of_task_id=retry_of_task_id,
    )
    db.add(task)
    try:
        db.flush()
        _create_generation_lineage(
            db,
            task=task,
            quote=quote,
            user_id=user_id,
            reverse_operation_id=reverse_operation_id,
            reverse_revision_id=reverse_revision_id,
        )
        reproduction_context = (
            dict(params.get("_reproduction_context") or {})
            if isinstance(params, dict)
            and isinstance(params.get("_reproduction_context"), dict)
            else {}
        )
        if reproduction_context:
            from . import reproduction_remediation

            reproduction_remediation.bind_generation_task(
                db,
                user_id=int(user_id),
                remediation_id=int(reproduction_context["remediation_id"]),
                plan_item_id=str(reproduction_context["plan_item_id"]),
                task_id=int(task.id),
                commit=False,
            )
        if project_id is not None:
            project = project_collection.require_owned_project(db, user_id, project_id)
            project_collection.attach_task(
                db,
                project=project,
                task_kind="generation",
                task_id=int(task.id),
            )
            input_urls: list[tuple[str | None, str]] = [
                (source_asset_url, "source"),
                (params.get("reference_image_url"), "reference"),
                (params.get("product_reference_image"), "product"),
                (params.get("first_frame_image"), "first_frame"),
                (params.get("last_frame_image"), "last_frame"),
                (params.get("style_reference_image"), "style"),
                (params.get("character_reference_image"), "character"),
                (params.get("mask_image_url"), "mask"),
            ]
            input_urls.extend(
                (url, "product_detail") for url in params.get("product_detail_images") or []
            )
            project_collection.attach_input_urls(
                db,
                project=project,
                user_id=user_id,
                inputs=input_urls,
            )
            db.flush()
        recipe_usage.record_generation_submit(
            db,
            attribution=recipe_attribution,
            user_id=user_id,
            quote_id=int(quote.id),
            task_id=int(task.id),
            category=category,
        )
        dispatch = create_dispatch_intent(db, task)
    except IntegrityError:
        db.rollback()
        replay = replay_conflicting_task()
        if replay is not None:
            return replay
        raise
    except HTTPException:
        db.rollback()
        raise
    except project_collection.ProjectNotFound as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc

    try:
        # Keep the quote-to-task binding, frozen credits, and task row in one
        # transaction. A balance failure rolls all three changes back, which
        # leaves an explicit, previously committed quote active for retry.
        consume_generation_quote(quote, task_id=task.id)
        credits.freeze(db, user_id, cost, biz_ref=task.id, commit=False)
    except (credits.InsufficientCredits, ValueError) as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        replay = replay_conflicting_task()
        if replay is not None:
            return replay
        raise
    db.refresh(task)

    _remember_generation_prompt(db, task, category=category, stage=stage)
    _enqueue_generation_task(
        db,
        task,
        category=category,
        user_id=user_id,
        cost=cost,
        dispatch_id=dispatch.id,
    )
    audit.log(
        db,
        user_id=user_id,
        action="generate",
        biz_type="gen_task",
        biz_id=task.id,
        ip=ip,
        detail={
            "category": category,
            "stage": stage,
            "cost": cost,
            "model_config_id": model_config_id,
            "reverse_operation_id": task.reverse_operation_id,
            "source_revision_id": task.source_revision_id,
            "compiled_revision_id": task.compiled_revision_id,
            "generation_revision_id": task.generation_revision_id,
            "creation_recipe": (
                recipe_attribution.snapshot() if recipe_attribution is not None else None
            ),
            "retry_of_task_id": task.retry_of_task_id,
            "retry": (
                dict(task.params.get("_retry") or {})
                if isinstance(task.params, dict) and isinstance(task.params.get("_retry"), dict)
                else None
            ),
        },
    )
    return build_task_out(db, task)


def _enqueue_generation_task(
    db: Session,
    task: GenTask,
    *,
    category: str,
    user_id: int,
    cost: int,
    dispatch_id: int,
) -> None:
    """Compatibility seam for tests; publication is owned by the outbox."""
    del task, category, user_id, cost
    from .generation_dispatch import publish_dispatch

    publish_dispatch(db, dispatch_id)


def _remember_generation_prompt(
    db: Session,
    task: GenTask,
    *,
    category: str,
    stage: str,
) -> None:
    try:
        prompt = task.prompt or {}
        final_prompt_text = str(prompt.get("final_text") or prompt.get("instruction") or "").strip()
        if final_prompt_text:
            remember_prompt(
                db,
                user_id=task.user_id,
                prompt=final_prompt_text,
                title=f"{'视频' if category == 'video' else '图片'}生成提示词",
                category=category,
                source="generate",
                params={"task_id": task.id, "stage": stage},
                commit=True,
            )
    except Exception:
        db.rollback()
