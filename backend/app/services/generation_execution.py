"""Transactional orchestration for initial generation and requote retries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GenerationQuote, GenTask, User
from ..schemas import GenerateIn, GenerationQuoteIn, RetryRequoteIn, TaskOut
from . import generation_retry, locks, recipe_usage
from .generation_policy import assert_retry_submission_state_known
from .generation_prepare import (
    PreparedGeneration,
    _existing_client_request_task,
    _normalize_client_request_id,
    _task_recipe_attribution,
)
from .generation_quotes import (
    create_generation_quote,
    lock_generation_quote,
    quoted_model_snapshot,
    validate_generation_quote,
)
from .generation_request import assert_client_request_replay
from .generation_submit import submit_generation_task
from .task_output import build_task_out


@dataclass(frozen=True)
class GenerationExecutionDependencies:
    prepare_generation: Callable[..., PreparedGeneration]
    find_active_final: Callable[[Session, int, int], GenTask | None]
    rate_limit: Callable[[int], None]


def _consumed_quote_replay(
    db: Session,
    *,
    user: User,
    body: GenerateIn,
    quote_task: GenTask,
    dependencies: GenerationExecutionDependencies,
) -> TaskOut:
    quote = db.scalar(
        select(GenerationQuote)
        .where(
            GenerationQuote.id == body.quote_id,
            GenerationQuote.user_id == user.id,
        )
        .with_for_update()
    )
    if quote is None:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_STATE_INVALID", "message": "生成任务的报价记录缺失"},
        )
    prepared = dependencies.prepare_generation(body, db=db, user=user, quote=quote)
    if (
        prepared.existing_client_task is None
        or int(prepared.existing_client_task.id) != int(quote_task.id)
        or quote.status != "consumed"
        or int(quote.task_id or 0) != int(quote_task.id)
    ):
        raise HTTPException(
            409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "报价已用于不同的生成请求",
            },
        )
    validate_generation_quote(
        db,
        quote,
        request_fingerprint=prepared.request_fingerprint,
        model=prepared.model,
    )
    return build_task_out(db, quote_task)


def _bind_quote_runtime(prepared: PreparedGeneration, quote: GenerationQuote) -> None:
    prepared.task_params["_model_snapshot"] = quoted_model_snapshot(quote, prepared.snapshot)
    prepared.task_params["_quote"] = {
        "quote_id": int(quote.id),
        "capability_version_id": int(quote.capability_version_id),
        "price_version_id": int(quote.price_version_id),
        "estimated_credits": int(quote.estimated_credits),
        "expires_at": quote.expires_at.isoformat(),
    }


def _generation_replay_callback(
    db: Session,
    *,
    user_id: int,
    body: GenerateIn,
    prepared: PreparedGeneration,
    quote: GenerationQuote,
    dependencies: GenerationExecutionDependencies,
) -> Callable[[], TaskOut | None]:
    def replay() -> TaskOut | None:
        existing = _existing_client_request_task(db, user_id, prepared.client_request_id)
        if existing is not None:
            assert_client_request_replay(existing, prepared.request_fingerprint)
            return build_task_out(db, existing)
        quote_task = db.scalar(
            select(GenTask).where(
                GenTask.user_id == user_id,
                GenTask.quote_id == quote.id,
            )
        )
        if quote_task is not None:
            return build_task_out(db, quote_task)
        if body.category == "video" and body.stage == "final" and prepared.parent is not None:
            active = dependencies.find_active_final(db, user_id, int(prepared.parent.id))
            if active is not None:
                return build_task_out(db, active)
        return None

    return replay


def _submit_prepared_generation(
    db: Session,
    *,
    user: User,
    body: GenerateIn,
    ip: str | None,
    prepared: PreparedGeneration,
    quote: GenerationQuote,
    dependencies: GenerationExecutionDependencies,
) -> TaskOut:
    return submit_generation_task(
        db,
        user_id=user.id,
        source_asset_url=prepared.source_asset_url,
        source_type=prepared.source_type,
        category=body.category,
        stage=body.stage,
        prompt=prepared.prompt,
        model_use=prepared.model_use,
        model_config_id=int(prepared.model.id),
        quote=quote,
        params=prepared.task_params,
        client_request_id=prepared.client_request_id,
        cost=int(quote.estimated_credits),
        parent_task_id=body.parent_task_id,
        project_id=prepared.project_id,
        reverse_operation_id=prepared.reverse_operation_id,
        reverse_revision_id=prepared.reverse_revision_id,
        recipe_attribution=prepared.recipe_attribution,
        ip=ip,
        replay_conflicting_task=_generation_replay_callback(
            db,
            user_id=int(user.id),
            body=body,
            prepared=prepared,
            quote=quote,
            dependencies=dependencies,
        ),
    )


def _submit_final_generation(
    db: Session,
    *,
    user: User,
    prepared: PreparedGeneration,
    dependencies: GenerationExecutionDependencies,
    submit: Callable[[], TaskOut],
) -> TaskOut:
    lock_key = f"gen:final:create:{user.id}:{prepared.parent.id}"
    lock_token = locks.acquire(lock_key, ttl=60)
    if not lock_token:
        active = dependencies.find_active_final(db, int(user.id), int(prepared.parent.id))
        if active:
            return build_task_out(db, active)
        raise HTTPException(409, "高清视频任务正在创建,请稍后刷新")
    try:
        active = dependencies.find_active_final(db, int(user.id), int(prepared.parent.id))
        return build_task_out(db, active) if active else submit()
    finally:
        locks.release(lock_key, lock_token)


def _execute_locked_generation(
    db: Session,
    *,
    user: User,
    body: GenerateIn,
    ip: str | None,
    dependencies: GenerationExecutionDependencies,
) -> TaskOut:
    quote_task = db.scalar(
        select(GenTask).where(
            GenTask.user_id == user.id,
            GenTask.quote_id == body.quote_id,
        )
    )
    if quote_task is not None:
        return _consumed_quote_replay(
            db,
            user=user,
            body=body,
            quote_task=quote_task,
            dependencies=dependencies,
        )

    quote = lock_generation_quote(db, quote_id=body.quote_id, user_id=user.id)
    prepared = dependencies.prepare_generation(body, db=db, user=user, quote=quote)
    if prepared.existing_client_task is not None:
        return build_task_out(db, prepared.existing_client_task)
    if prepared.parent is not None:
        active = dependencies.find_active_final(db, int(user.id), int(prepared.parent.id))
        if active is not None:
            return build_task_out(db, active)
    dependencies.rate_limit(int(user.id))
    validate_generation_quote(
        db,
        quote,
        request_fingerprint=prepared.request_fingerprint,
        model=prepared.model,
    )
    _bind_quote_runtime(prepared, quote)
    def submit() -> TaskOut:
        return _submit_prepared_generation(
            db,
            user=user,
            body=body,
            ip=ip,
            prepared=prepared,
            quote=quote,
            dependencies=dependencies,
        )
    if body.category == "video" and body.stage == "final" and prepared.parent is not None:
        return _submit_final_generation(
            db,
            user=user,
            prepared=prepared,
            dependencies=dependencies,
            submit=submit,
        )
    return submit()


def execute_generation(
    db: Session,
    *,
    user: User,
    body: GenerateIn,
    ip: str | None,
    dependencies: GenerationExecutionDependencies,
) -> TaskOut:
    quote_lock_key = f"gen:quote:consume:{user.id}:{body.quote_id}"
    quote_lock_token = locks.acquire(quote_lock_key, ttl=60)
    if not quote_lock_token:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_IN_USE", "message": "报价正在使用，请稍后刷新"},
        )
    try:
        return _execute_locked_generation(
            db,
            user=user,
            body=body,
            ip=ip,
            dependencies=dependencies,
        )
    finally:
        locks.release(quote_lock_key, quote_lock_token)


@dataclass(frozen=True)
class _RequoteContext:
    source: GenTask
    client_request_id: str
    selected_model_config_id: int
    retry_body: GenerationQuoteIn
    prepared: PreparedGeneration


def _prepare_requote(
    db: Session,
    *,
    user: User,
    task_id: int,
    body: RetryRequoteIn,
    dependencies: GenerationExecutionDependencies,
) -> _RequoteContext | TaskOut:
    source = db.scalar(
        select(GenTask)
        .where(GenTask.id == task_id, GenTask.user_id == user.id)
        .with_for_update()
    )
    if source is None:
        raise HTTPException(404, "任务不存在")
    if source.status != "failed":
        raise HTTPException(400, "仅失败任务可重新报价重试")
    client_request_id = _normalize_client_request_id(body.client_request_id)
    selected_model_config_id = generation_retry.requote_model_config_id(
        source,
        body.model_config_id,
    )
    existing = _existing_client_request_task(db, int(user.id), client_request_id)
    if existing is not None:
        generation_retry.assert_requote_replay(
            existing,
            source_task_id=int(source.id),
            selected_model_config_id=selected_model_config_id,
        )
        return build_task_out(db, existing)
    assert_retry_submission_state_known(source.params)
    generation_retry.assert_retry_dispatch_reconciled(db, source)
    generation_retry.assert_retry_generation_lineage(db, source)
    retry_body = generation_retry.build_requote_input(
        db,
        task=source,
        user_id=int(user.id),
        client_request_id=str(client_request_id),
        model_config_id=selected_model_config_id,
    )
    old_route_id = generation_retry.source_route_id(source)
    prepared = dependencies.prepare_generation(
        retry_body,
        db=db,
        user=user,
        fresh_route=True,
        avoid_route_ids={old_route_id} if old_route_id is not None else None,
        trusted_recipe_attribution=_task_recipe_attribution(source),
    )
    if prepared.existing_client_task is not None:
        generation_retry.assert_requote_replay(
            prepared.existing_client_task,
            source_task_id=int(source.id),
            selected_model_config_id=selected_model_config_id,
        )
        return build_task_out(db, prepared.existing_client_task)
    dependencies.rate_limit(int(user.id))
    return _RequoteContext(
        source=source,
        client_request_id=str(client_request_id),
        selected_model_config_id=selected_model_config_id,
        retry_body=retry_body,
        prepared=prepared,
    )


def _retry_metadata(db: Session, context: _RequoteContext) -> dict:
    source = context.source
    prepared = context.prepared
    route_snapshot = (
        prepared.snapshot.get("route_snapshot")
        if isinstance(prepared.snapshot.get("route_snapshot"), dict)
        else {}
    )
    old_route_id = generation_retry.source_route_id(source)
    new_route_id = route_snapshot.get("route_id")
    source_quote = db.get(GenerationQuote, source.quote_id) if source.quote_id is not None else None
    source_model_config_id = (
        int(source.model_config_id) if source.model_config_id is not None else None
    )
    return {
        "mode": "requote",
        "source_task_id": int(source.id),
        "source_model_config_id": source_model_config_id,
        "selected_model_config_id": int(prepared.model.id),
        "model_switched": source_model_config_id != int(prepared.model.id),
        "source_quote_id": int(source.quote_id) if source.quote_id is not None else None,
        "source_estimated_credits": (
            int(source_quote.estimated_credits)
            if source_quote is not None
            else int(source.cost_frozen or 0)
        ),
        "source_route_id": old_route_id,
        "selected_route_id": int(new_route_id) if new_route_id is not None else None,
    }


def _create_retry_quote(
    db: Session,
    *,
    user: User,
    context: _RequoteContext,
) -> GenerationQuote:
    prepared = context.prepared
    retry_meta = _retry_metadata(db, context)
    prepared.request_snapshot["retry"] = dict(retry_meta)
    prepared.task_params["_retry"] = dict(retry_meta)
    quote = create_generation_quote(
        db,
        user_id=user.id,
        model=prepared.model,
        request_fingerprint=prepared.request_fingerprint,
        category=context.retry_body.category,
        stage=context.retry_body.stage,
        request_snapshot=prepared.request_snapshot,
        model_snapshot=prepared.snapshot,
        params=prepared.task_params,
        source_type=prepared.source_type,
        estimated_credits=prepared.cost,
    )
    recipe_usage.record_generation_prepare(
        db,
        attribution=prepared.recipe_attribution,
        user_id=int(user.id),
        quote_id=int(quote.id),
        category=context.retry_body.category,
    )
    retry_meta["selected_quote_id"] = int(quote.id)
    retry_meta["selected_estimated_credits"] = int(quote.estimated_credits)
    quote.request_snapshot = {
        **dict(quote.request_snapshot or {}),
        "retry": dict(retry_meta),
    }
    prepared.task_params["_retry"] = dict(retry_meta)
    _bind_quote_runtime(prepared, quote)
    return quote


def _retry_replay_callback(
    db: Session,
    *,
    user_id: int,
    context: _RequoteContext,
) -> Callable[[], TaskOut | None]:
    def replay() -> TaskOut | None:
        existing = _existing_client_request_task(db, user_id, context.client_request_id)
        if existing is None:
            return None
        generation_retry.assert_requote_replay(
            existing,
            source_task_id=int(context.source.id),
            selected_model_config_id=context.selected_model_config_id,
        )
        return build_task_out(db, existing)

    return replay


def _submit_retry(
    db: Session,
    *,
    user: User,
    context: _RequoteContext,
    quote: GenerationQuote,
    ip: str | None,
) -> TaskOut:
    prepared = context.prepared
    retry_body = context.retry_body
    return submit_generation_task(
        db,
        user_id=user.id,
        source_asset_url=prepared.source_asset_url,
        source_type=prepared.source_type,
        category=retry_body.category,
        stage=retry_body.stage,
        prompt=prepared.prompt,
        model_use=prepared.model_use,
        model_config_id=int(prepared.model.id),
        quote=quote,
        params=prepared.task_params,
        client_request_id=prepared.client_request_id,
        cost=int(quote.estimated_credits),
        parent_task_id=retry_body.parent_task_id,
        project_id=prepared.project_id,
        reverse_operation_id=prepared.reverse_operation_id,
        reverse_revision_id=prepared.reverse_revision_id,
        recipe_attribution=prepared.recipe_attribution,
        ip=ip,
        replay_conflicting_task=_retry_replay_callback(
            db,
            user_id=int(user.id),
            context=context,
        ),
        retry_of_task_id=int(context.source.id),
    )


def requote_retry_task(
    db: Session,
    *,
    user: User,
    task_id: int,
    body: RetryRequoteIn,
    ip: str | None,
    dependencies: GenerationExecutionDependencies,
) -> TaskOut:
    prepared = _prepare_requote(
        db,
        user=user,
        task_id=task_id,
        body=body,
        dependencies=dependencies,
    )
    if not isinstance(prepared, _RequoteContext):
        return prepared
    quote = _create_retry_quote(db, user=user, context=prepared)
    return _submit_retry(db, user=user, context=prepared, quote=quote, ip=ip)
