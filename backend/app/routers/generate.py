"""Submit generation tasks. Freezes the estimated cost, enqueues the worker,
and refunds immediately if enqueue fails."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import GenAsset, GenerationQuote, GenTask, User
from ..schemas import (
    ExecutionQuoteIn,
    GenerateIn,
    GenerationQuoteIn,
    GenerationQuoteOut,
    GenerationRequestBase,
    RetryRequoteIn,
    TaskOut,
)
from ..services import (
    generation,
    project_collection,
    recipe_usage,
    reverse_operations,
)
from ..services.generation_execution import (
    GenerationExecutionDependencies,
    execute_generation,
    requote_retry_task,
)
from ..services.generation_model_runtime import model_from_persisted_snapshot
from ..services.generation_prepare import (
    PreparedGeneration,
    _GenerationPrepareDependencies,
    _prepare_generation_with_dependencies,
)
from ..services.generation_quote_requests import create_requested_execution_quote
from ..services.generation_quotes import (
    create_generation_quote,
    find_idempotent_quote,
    generation_quote_out,
)
from ..services.product_edition import feature_enabled
from ..services.rate_limit import incr_window

router = APIRouter(prefix="/api", tags=["generate"])
model_snapshot = generation.model_snapshot


def _rate_limit(user_id: int) -> None:
    key = f"gen:rate:{user_id}"
    n = incr_window(key, 3600)
    if n > settings.user_gen_rate_per_hour:
        raise HTTPException(429, "生成过于频繁,请稍后再试")


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


def _prepare_generation(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
    quote: GenerationQuote | None = None,
    fresh_route: bool = False,
    avoid_route_ids: set[int] | None = None,
    trusted_recipe_attribution: recipe_usage.RecipeAttribution | None = None,
) -> PreparedGeneration:
    """Compatibility wrapper for the generation preparation service."""
    return _prepare_generation_with_dependencies(
        body,
        db=db,
        user=user,
        quote=quote,
        fresh_route=fresh_route,
        avoid_route_ids=avoid_route_ids,
        trusted_recipe_attribution=trusted_recipe_attribution,
        dependencies=_GenerationPrepareDependencies(
            snapshot_builder=model_snapshot,
            snapshot_loader=model_from_persisted_snapshot,
        ),
    )


def _execution_dependencies() -> GenerationExecutionDependencies:
    return GenerationExecutionDependencies(
        prepare_generation=_prepare_generation,
        find_active_final=_existing_active_final,
        rate_limit=_rate_limit,
    )


def _quote_rate_limit(user_id: int) -> None:
    key = f"gen:quote:rate:{user_id}"
    if incr_window(key, 3600) > max(100, int(settings.user_gen_rate_per_hour) * 5):
        raise HTTPException(429, "报价请求过于频繁,请稍后再试")


def _quote_generation_intent(
    body: GenerationQuoteIn,
    *,
    db: Session,
    user: User,
):
    prepared = _prepare_generation(body, db=db, user=user)
    if prepared.existing_client_task is not None:
        existing_quote_id = prepared.existing_client_task.quote_id
        if existing_quote_id is not None:
            existing_quote = db.get(GenerationQuote, existing_quote_id)
            if existing_quote is not None:
                return existing_quote
        raise HTTPException(
            409,
            detail={"code": "TASK_ALREADY_EXISTS", "message": "该请求已创建生成任务"},
        )
    if prepared.parent is not None:
        existing_final = _existing_active_final(db, user.id, prepared.parent.id)
        if existing_final is not None:
            if existing_final.quote_id is not None:
                existing_quote = db.get(GenerationQuote, existing_final.quote_id)
                if existing_quote is not None:
                    return existing_quote
            raise HTTPException(
                409,
                detail={"code": "TASK_ALREADY_EXISTS", "message": "该预览已创建最终生成任务"},
            )
    replay = find_idempotent_quote(
        db,
        user_id=user.id,
        kind="generation",
        client_request_id=body.client_request_id,
        request_fingerprint=prepared.request_fingerprint,
    )
    if replay is not None:
        return replay
    _quote_rate_limit(user.id)
    return create_generation_quote(
        db,
        user_id=user.id,
        model=prepared.model,
        request_fingerprint=prepared.request_fingerprint,
        category=body.category,
        stage=body.stage,
        request_snapshot=prepared.request_snapshot,
        model_snapshot=prepared.snapshot,
        params=prepared.task_params,
        source_type=prepared.source_type,
        estimated_credits=prepared.cost,
    )


@router.post("/quotes", response_model=GenerationQuoteOut, status_code=201)
def quote_generation(
    body: GenerationQuoteIn | ExecutionQuoteIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if isinstance(body, ExecutionQuoteIn) and (
        (body.kind == "reverse_batch" and not feature_enabled("reverse_batch_enabled"))
        or (body.kind == "workflow" and not feature_enabled("tool_workflows_enabled"))
    ):
        raise HTTPException(404, "not found")
    if isinstance(body, GenerationQuoteIn):
        quote = _quote_generation_intent(body, db=db, user=user)
    else:
        _quote_rate_limit(user.id)
        try:
            quote = create_requested_execution_quote(
                db,
                user_id=user.id,
                body=body,
                generation_quote_factory=lambda generation_body: _quote_generation_intent(
                    generation_body,
                    db=db,
                    user=user,
                ),
            )
        except reverse_operations.ReverseOperationNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except reverse_operations.ReverseOperationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except reverse_operations.ReverseOperationInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        except project_collection.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
    if quote.kind == "generation":
        snapshot = quote.request_snapshot if isinstance(quote.request_snapshot, dict) else {}
        recipe_usage.record_generation_prepare(
            db,
            attribution=recipe_usage.attribution_from_snapshot(snapshot.get("creation_recipe")),
            user_id=int(user.id),
            quote_id=int(quote.id),
            category=quote.category,
        )
    db.commit()
    db.refresh(quote)
    return generation_quote_out(db, quote)


@router.post("/generate", response_model=TaskOut)
def generate(
    body: GenerateIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = execute_generation(
        db,
        user=user,
        body=body,
        ip=get_client_ip(request),
        dependencies=_execution_dependencies(),
    )
    if result.dispatch_reconciliation_required:
        response.status_code = 202
    return result


@router.post("/tasks/{task_id}/retry/requote", response_model=TaskOut)
def requote_retry_generation(
    task_id: int,
    body: RetryRequoteIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new task using the failed task's public request and current quote."""
    result = requote_retry_task(
        db,
        user=user,
        task_id=task_id,
        body=body,
        ip=get_client_ip(request),
        dependencies=_execution_dependencies(),
    )
    if result.dispatch_reconciliation_required:
        response.status_code = 202
    return result
