"""Versioned model-route selection, immutable snapshots, and circuit health."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ModelConfig, ModelRoute, ModelRouteHealthEvent, ModelRouteVersion
from .model_gateway_config import apply_model_gateway_update

ROUTE_SNAPSHOT_SCHEMA_VERSION = "model-route.v2"
LEGACY_ROUTE_SNAPSHOT_SCHEMA_VERSION = "model-route.v1"
_ROUTE_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PROTECTED_ROUTE_EXTRA_KEYS = frozenset({"capabilities", "credit_pricing"})
_SECRET_ROUTE_EXTRA_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "proxy_authorization",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
        "cookie",
        "set_cookie",
    }
)
_ROUTE_CONFIG_FIELDS = (
    "name",
    "model_id",
    "provider",
    "base_url",
    "api_key_encrypted",
    "gateway_format",
    "extra",
    "priority",
    "enabled",
    "managed_by_model_config",
    "failure_threshold",
    "window_seconds",
    "cooldown_seconds",
)


class ModelRouteUnavailable(RuntimeError):
    pass


class ModelRouteSnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class RouteSelection:
    route: ModelRoute
    route_version: ModelRouteVersion
    runtime_model: Any
    selected_at: datetime
    selection_state: str
    candidate_count: int


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat() if normalized is not None else None


def _managed_values(model: ModelConfig) -> dict[str, Any]:
    return {
        "provider": model.provider,
        "base_url": model.base_url,
        "api_key_encrypted": model.api_key_encrypted,
        "gateway_format": model.gateway_format,
        "enabled": bool(model.enabled),
    }


def route_config_snapshot(route: ModelRoute) -> dict[str, Any]:
    values = {key: deepcopy(getattr(route, key)) for key in _ROUTE_CONFIG_FIELDS}
    values["extra"] = deepcopy(values.get("extra") or {})
    values["priority"] = int(values.get("priority") or 0)
    values["enabled"] = bool(values.get("enabled"))
    values["managed_by_model_config"] = bool(values.get("managed_by_model_config"))
    values["failure_threshold"] = max(1, int(values.get("failure_threshold") or 1))
    values["window_seconds"] = max(1, int(values.get("window_seconds") or 1))
    values["cooldown_seconds"] = max(1, int(values.get("cooldown_seconds") or 1))
    return values


def _apply_route_config(
    route: ModelRoute,
    values: dict[str, Any],
    *,
    preserve_live_credential: bool = False,
) -> None:
    for key in _ROUTE_CONFIG_FIELDS:
        if preserve_live_credential and key == "api_key_encrypted":
            continue
        if key in values:
            setattr(route, key, deepcopy(values[key]))


def _next_route_version(db: Session, route_id: int) -> int:
    current = db.scalar(
        select(func.max(ModelRouteVersion.version)).where(
            ModelRouteVersion.route_id == int(route_id)
        )
    )
    return int(current or 0) + 1


def active_route_version(
    db: Session,
    route: ModelRoute,
    *,
    create: bool = True,
) -> ModelRouteVersion | None:
    active = db.scalar(
        select(ModelRouteVersion).where(
            ModelRouteVersion.route_id == int(route.id),
            ModelRouteVersion.is_active.is_(True),
        )
    )
    if active is not None or not create:
        return active
    now = datetime.now(timezone.utc)
    version = max(int(route.config_revision or 1), _next_route_version(db, int(route.id)))
    active = ModelRouteVersion(
        route_id=int(route.id),
        version=version,
        config_snapshot=route_config_snapshot(route),
        status="published",
        is_active=True,
        activated_at=now,
    )
    route.config_revision = version
    db.add(active)
    db.flush()
    return active


def publish_route_projection(
    db: Session,
    route: ModelRoute,
    *,
    source_version_id: int | None = None,
) -> ModelRouteVersion:
    """Append the current route projection as a new immutable published version."""
    current = active_route_version(db, route, create=False)
    snapshot = route_config_snapshot(route)
    if (
        source_version_id is None
        and current is not None
        and current.status == "published"
        and current.is_active
        and dict(current.config_snapshot or {}) == snapshot
    ):
        route.config_revision = int(current.version)
        return current
    now = datetime.now(timezone.utc)
    if current is not None:
        current.is_active = False
        current.status = "disabled"
        current.disabled_at = now
        db.flush()
    version = ModelRouteVersion(
        route_id=int(route.id),
        version=_next_route_version(db, int(route.id)),
        config_snapshot=snapshot,
        status="published",
        is_active=True,
        source_version_id=source_version_id,
        activated_at=now,
    )
    db.add(version)
    db.flush()
    route.config_revision = int(version.version)
    db.flush()
    return version


def route_version_row(
    db: Session,
    route_id: int,
    version: int,
    *,
    for_update: bool = False,
) -> ModelRouteVersion | None:
    query = select(ModelRouteVersion).where(
        ModelRouteVersion.route_id == int(route_id),
        ModelRouteVersion.version == int(version),
    )
    if for_update:
        query = query.with_for_update()
    return db.scalar(query)


def route_version_history(db: Session, route: ModelRoute) -> list[ModelRouteVersion]:
    return list(
        db.scalars(
            select(ModelRouteVersion)
            .where(ModelRouteVersion.route_id == int(route.id))
            .order_by(ModelRouteVersion.version.desc())
        )
    )


def rollback_route_version(
    db: Session,
    route: ModelRoute,
    *,
    version: int,
) -> ModelRouteVersion:
    source = route_version_row(db, int(route.id), version, for_update=True)
    if source is None:
        raise LookupError("模型路由版本不存在")
    if source.status != "disabled" or source.is_active:
        raise ValueError("只能回滚到已停用且未退役的历史路由版本")
    # A rollback restores the routable configuration but never revives an old
    # credential. Secrets are operational state and old ciphertext must not
    # become live again through a catalog-history action.
    _apply_route_config(
        route,
        dict(source.config_snapshot or {}),
        preserve_live_credential=True,
    )
    return publish_route_projection(db, route, source_version_id=int(source.id))


def retire_route_version(
    db: Session,
    route: ModelRoute,
    *,
    version: int,
) -> ModelRouteVersion:
    target = route_version_row(db, int(route.id), version, for_update=True)
    if target is None:
        raise LookupError("模型路由版本不存在")
    if target.is_active:
        raise ValueError("当前路由版本不能退役")
    if target.status == "retired":
        return target
    if target.status not in {"draft", "disabled"}:
        raise ValueError("只有草稿或已停用路由版本可以退役")
    target.status = "retired"
    target.retired_at = datetime.now(timezone.utc)
    db.flush()
    return target


def serialize_route_version(row: ModelRouteVersion) -> dict[str, Any]:
    config = deepcopy(row.config_snapshot or {})
    api_key_configured = bool(config.pop("api_key_encrypted", None))
    return {
        "id": int(row.id),
        "route_id": int(row.route_id),
        "version": int(row.version),
        "schema_version": row.schema_version,
        "config": config,
        "api_key_configured": api_key_configured,
        "status": row.status,
        "is_active": bool(row.is_active),
        "source_version_id": row.source_version_id,
        "activated_at": row.activated_at,
        "disabled_at": row.disabled_at,
        "retired_at": row.retired_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _sync_managed_route(db: Session, route: ModelRoute, model: ModelConfig) -> bool:
    if not route.managed_by_model_config:
        active_route_version(db, route)
        return False
    values = _managed_values(model)
    changed = any(getattr(route, key) != value for key, value in values.items())
    for key, value in values.items():
        setattr(route, key, value)
    if changed:
        publish_route_projection(db, route)
    else:
        active_route_version(db, route)
    return changed


def ensure_legacy_route(db: Session, model: ModelConfig) -> ModelRoute:
    route = db.scalar(
        select(ModelRoute).where(
            ModelRoute.model_config_id == model.id,
            ModelRoute.route_key == "legacy-default",
        )
    )
    if route is None:
        route = ModelRoute(
            model_config_id=int(model.id),
            route_key="legacy-default",
            name=f"{model.display_name or model.model_id} 默认路由",
            priority=100,
            enabled=bool(model.enabled),
            managed_by_model_config=True,
            extra={},
        )
        for key, value in _managed_values(model).items():
            setattr(route, key, value)
        db.add(route)
        db.flush()
        active_route_version(db, route)
    else:
        _sync_managed_route(db, route, model)
        db.flush()
    return route


def route_runtime_model(
    model: ModelConfig,
    route: ModelRoute,
    version: ModelRouteVersion | None = None,
) -> Any:
    config = dict(version.config_snapshot or {}) if version is not None else route_config_snapshot(route)
    base_extra = deepcopy(model.extra) if isinstance(model.extra, dict) else {}
    route_extra = deepcopy(config.get("extra")) if isinstance(config.get("extra"), dict) else {}
    for key in _PROTECTED_ROUTE_EXTRA_KEYS:
        route_extra.pop(key, None)
    base_extra.update(route_extra)
    has_route_gateway = bool(config.get("base_url") or config.get("api_key_encrypted"))
    return SimpleNamespace(
        id=int(model.id),
        display_name=model.display_name,
        model_id=str(config.get("model_id") or model.model_id),
        use=model.use,
        cost_credits=int(model.cost_credits or 0),
        unlock_cost=int(model.unlock_cost or 0),
        enabled=bool(model.enabled and config.get("enabled")),
        extra=base_extra,
        provider=config.get("provider"),
        base_url=config.get("base_url"),
        api_key_encrypted=config.get("api_key_encrypted"),
        gateway_format=config.get("gateway_format"),
        gateway_source="model" if has_route_gateway else "env",
        route_id=int(route.id),
        route_key=route.route_key,
        route_name=config.get("name") or route.name,
        route_config_revision=int(version.version if version is not None else route.config_revision),
        route_version_id=int(version.id) if version is not None else None,
        route_version=int(version.version) if version is not None else int(route.config_revision),
    )


def _route_eligible_state(route: ModelRoute, now: datetime) -> str | None:
    if not route.enabled:
        return None
    if route.health_status == "closed":
        return "closed"
    if route.health_status == "open":
        cooldown_until = _utc(route.cooldown_until)
        return "half_open" if cooldown_until is None or cooldown_until <= now else None
    claimed_until = _utc(route.half_open_claimed_until)
    return "half_open" if claimed_until is None or claimed_until <= now else None


def select_model_route(
    db: Session,
    model: ModelConfig,
    *,
    avoid_route_ids: set[int] | None = None,
) -> RouteSelection:
    ensure_legacy_route(db, model)
    routes = list(
        db.scalars(
            select(ModelRoute)
            .where(ModelRoute.model_config_id == model.id)
            .order_by(ModelRoute.priority, ModelRoute.id)
            .with_for_update()
        )
    )
    versions: dict[int, ModelRouteVersion] = {}
    for route in routes:
        _sync_managed_route(db, route, model)
        version = active_route_version(db, route)
        if version is not None:
            versions[int(route.id)] = version
    now = datetime.now(timezone.utc)
    candidates: list[tuple[ModelRoute, ModelRouteVersion, str]] = []
    for route in routes:
        state = _route_eligible_state(route, now)
        version = versions.get(int(route.id))
        if state is not None and version is not None:
            candidates.append((route, version, state))
    if not candidates:
        raise ModelRouteUnavailable("所选模型的全部供应商路由均已停用或熔断,请稍后重试")

    avoided = {int(route_id) for route_id in (avoid_route_ids or set())}
    # Requoted retries prefer a different healthy route. If no alternative is
    # available the avoided route remains eligible, which keeps single-route
    # models usable. Closed routes win state ties to avoid needless probes.
    route, route_version, state = min(
        candidates,
        key=lambda item: (
            0 if int(item[0].id) not in avoided else 1,
            int(item[0].priority or 0),
            0 if item[2] == "closed" else 1,
            int(item[0].id),
        ),
    )
    if state == "half_open":
        route.health_status = "half_open"
        route.last_probe_at = now
        route.half_open_claimed_until = now + timedelta(seconds=60)
    db.flush()
    return RouteSelection(
        route=route,
        route_version=route_version,
        runtime_model=route_runtime_model(model, route, route_version),
        selected_at=now,
        selection_state=state,
        candidate_count=len(candidates),
    )


def attach_route_snapshot(snapshot: dict, selection: RouteSelection) -> dict:
    values = deepcopy(snapshot)
    route = selection.route
    route_version = selection.route_version
    values["route_snapshot"] = {
        "schema_version": ROUTE_SNAPSHOT_SCHEMA_VERSION,
        "route_id": int(route.id),
        "model_config_id": int(route.model_config_id),
        "route_key": route.route_key,
        "route_name": route.name,
        "route_version_id": int(route_version.id),
        "route_version": int(route_version.version),
        "config_revision": int(route_version.version),
        "priority": int(route.priority),
        "selected_at": selection.selected_at.isoformat(),
        "selection_state": selection.selection_state,
        "candidate_count": int(selection.candidate_count),
        "model_id": values.get("model_id"),
        "provider": values.get("provider"),
        "base_url": values.get("base_url"),
        "gateway_format": values.get("gateway_format"),
        "gateway_source": values.get("gateway_source"),
        "gateway_key_fingerprint": values.get("gateway_key_fingerprint"),
        "health": {
            "status": route.health_status,
            "window_requests": int(route.window_requests or 0),
            "window_failures": int(route.window_failures or 0),
            "consecutive_failures": int(route.consecutive_failures or 0),
            "latency_ema_ms": route.latency_ema_ms,
            "cooldown_until": _iso(route.cooldown_until),
        },
    }
    return values


def route_model_for_snapshot(
    db: Session,
    model: ModelConfig,
    snapshot: dict,
) -> Any:
    route_snapshot = snapshot.get("route_snapshot") if isinstance(snapshot, dict) else None
    if route_snapshot is None:
        return model
    if not isinstance(route_snapshot, dict):
        raise ModelRouteSnapshotError("报价路由快照格式非法")
    schema_version = route_snapshot.get("schema_version")
    if schema_version not in {
        ROUTE_SNAPSHOT_SCHEMA_VERSION,
        LEGACY_ROUTE_SNAPSHOT_SCHEMA_VERSION,
    }:
        raise ModelRouteSnapshotError("报价路由快照版本不受支持")
    try:
        route_id = int(route_snapshot["route_id"])
        snapshot_model_id = int(route_snapshot["model_config_id"])
        config_revision = int(route_snapshot["config_revision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelRouteSnapshotError("报价路由快照缺少有效标识") from exc
    route = db.get(ModelRoute, route_id)
    if (
        route is None
        or int(route.model_config_id) != int(model.id)
        or snapshot_model_id != int(model.id)
        or config_revision < 1
    ):
        raise ModelRouteSnapshotError("报价绑定的供应商路由不存在或不属于当前模型")
    if route.route_key != route_snapshot.get("route_key"):
        raise ModelRouteSnapshotError("报价绑定的供应商路由标识已变化")
    version_row = None
    route_version_id = route_snapshot.get("route_version_id")
    if route_version_id is not None:
        try:
            version_row = db.get(ModelRouteVersion, int(route_version_id))
        except (TypeError, ValueError) as exc:
            raise ModelRouteSnapshotError("报价绑定的路由版本标识非法") from exc
        if (
            version_row is None
            or int(version_row.route_id) != route_id
            or int(version_row.version) != config_revision
        ):
            raise ModelRouteSnapshotError("报价绑定的供应商路由版本不存在或不匹配")
    else:
        version_row = db.scalar(
            select(ModelRouteVersion).where(
                ModelRouteVersion.route_id == route_id,
                ModelRouteVersion.version == config_revision,
            )
        )
        if version_row is None and schema_version == ROUTE_SNAPSHOT_SCHEMA_VERSION:
            raise ModelRouteSnapshotError("报价绑定的供应商路由版本不存在")
    if version_row is not None and not version_row.is_active:
        raise ModelRouteSnapshotError("报价绑定的供应商路由版本已被替换")
    for key in (
        "model_id",
        "provider",
        "base_url",
        "gateway_format",
        "gateway_source",
        "gateway_key_fingerprint",
    ):
        if route_snapshot.get(key) != snapshot.get(key):
            raise ModelRouteSnapshotError(f"报价路由快照字段 {key} 与模型快照不一致")
    runtime_model = route_runtime_model(model, route, version_row)
    # Version rows freeze non-secret routing fields for reproducibility. The
    # credential must always come from the live route so rotating or clearing a
    # key invalidates previously quoted fingerprints instead of reviving an old
    # encrypted secret from history.
    runtime_model.api_key_encrypted = (
        model.api_key_encrypted
        if route.managed_by_model_config
        else route.api_key_encrypted
    )
    return runtime_model


def route_admin_dict(
    route: ModelRoute,
    version: ModelRouteVersion | None = None,
) -> dict[str, Any]:
    return {
        "id": int(route.id),
        "model_config_id": int(route.model_config_id),
        "route_key": route.route_key,
        "name": route.name,
        "model_id": route.model_id,
        "provider": route.provider,
        "base_url": route.base_url,
        "gateway_format": route.gateway_format,
        "api_key_configured": bool(route.api_key_encrypted),
        "extra": deepcopy(route.extra or {}),
        "priority": int(route.priority),
        "enabled": bool(route.enabled),
        "managed_by_model_config": bool(route.managed_by_model_config),
        "config_revision": int(route.config_revision),
        "failure_threshold": int(route.failure_threshold),
        "window_seconds": int(route.window_seconds),
        "cooldown_seconds": int(route.cooldown_seconds),
        "health_status": route.health_status,
        "window_requests": int(route.window_requests or 0),
        "window_failures": int(route.window_failures or 0),
        "consecutive_failures": int(route.consecutive_failures or 0),
        "opened_at": route.opened_at,
        "cooldown_until": route.cooldown_until,
        "last_probe_at": route.last_probe_at,
        "last_success_at": route.last_success_at,
        "last_failure_at": route.last_failure_at,
        "latency_ema_ms": route.latency_ema_ms,
        "last_error_code": route.last_error_code,
        "created_at": route.created_at,
        "updated_at": route.updated_at,
        "active_version": serialize_route_version(version) if version is not None else None,
    }


def model_route_summary(db: Session, model: ModelConfig) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(ModelRoute)
            .where(ModelRoute.model_config_id == model.id)
            .order_by(ModelRoute.priority, ModelRoute.id)
        )
    )
    if not rows:
        return {
            "configured": 1,
            "available": 1 if model.enabled else 0,
            "status": "available" if model.enabled else "unavailable",
            "legacy": True,
        }
    enabled_rows = [row for row in rows if row.enabled]
    now = datetime.now(timezone.utc)
    available = sum(_route_eligible_state(row, now) is not None for row in enabled_rows)
    return {
        "configured": len(rows),
        "available": int(available),
        "status": "available" if available else "unavailable",
        "legacy": False,
    }


def validate_route_key(value: str) -> str:
    normalized = str(value or "").strip()
    if not _ROUTE_KEY_RE.fullmatch(normalized):
        raise ValueError("路由标识只能使用小写字母、数字和连字符")
    return normalized


def validate_route_extra(value: dict | None) -> dict:
    result = deepcopy(value or {})
    protected = _PROTECTED_ROUTE_EXTRA_KEYS.intersection(result)
    if protected:
        raise ValueError("路由不能覆盖模型能力或价格字段: " + ",".join(sorted(protected)))

    def reject_secrets(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in _SECRET_ROUTE_EXTRA_KEYS:
                    raise ValueError(f"{path} 不能包含密钥字段 {key}")
                reject_secrets(nested, f"{path}.{key}")
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                reject_secrets(nested, f"{path}[{index}]")

    reject_secrets(result, "模型路由 extra")
    return result


def apply_route_gateway_update(
    route: ModelRoute,
    *,
    model_use: str,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    api_key_clear: bool,
    gateway_format: str | None,
) -> None:
    # The shared gateway validator only relies on these duck-typed fields.
    route.use = model_use
    apply_model_gateway_update(
        route,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        api_key_clear=api_key_clear,
        gateway_format=gateway_format,
    )


def reset_route_health(route: ModelRoute) -> None:
    route.health_status = "closed"
    route.window_started_at = None
    route.window_requests = 0
    route.window_failures = 0
    route.consecutive_failures = 0
    route.opened_at = None
    route.cooldown_until = None
    route.half_open_claimed_until = None
    route.last_error_code = None


def error_counts_toward_circuit(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if bool(getattr(exc, "transient", False)):
        return True
    return status_code in {401, 403, 408, 409, 425, 429} or (
        isinstance(status_code, int) and status_code >= 500
    )


def _safe_error_code(value: str | None) -> str | None:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())[:64]
    return normalized or None


def record_route_outcome(
    route_id: int | None,
    *,
    operation: str,
    success: bool,
    latency_ms: int | None = None,
    error_code: str | None = None,
    counts_toward_circuit: bool = True,
) -> None:
    if route_id is None:
        return
    now = datetime.now(timezone.utc)
    try:
        with SessionLocal() as db:
            route = db.scalar(
                select(ModelRoute).where(ModelRoute.id == int(route_id)).with_for_update()
            )
            if route is None:
                return
            latency = max(0, int(latency_ms)) if latency_ms is not None else None
            db.add(
                ModelRouteHealthEvent(
                    route_id=int(route.id),
                    operation=str(operation or "gateway")[:32],
                    outcome="success"
                    if success
                    else ("failure" if counts_toward_circuit else "ignored"),
                    counts_toward_circuit=bool(not success and counts_toward_circuit),
                    latency_ms=latency,
                    error_code=_safe_error_code(error_code),
                )
            )
            if latency is not None:
                previous = route.latency_ema_ms
                route.latency_ema_ms = (
                    latency if previous is None else round(previous * 0.8 + latency * 0.2)
                )
            if success:
                route.last_success_at = now
                route.health_status = "closed"
                route.window_started_at = now
                route.window_requests = 1
                route.window_failures = 0
                route.consecutive_failures = 0
                route.opened_at = None
                route.cooldown_until = None
                route.half_open_claimed_until = None
                route.last_error_code = None
            elif counts_toward_circuit:
                window_started = _utc(route.window_started_at)
                if (
                    window_started is None
                    or window_started + timedelta(seconds=int(route.window_seconds)) <= now
                ):
                    route.window_started_at = now
                    route.window_requests = 0
                    route.window_failures = 0
                route.window_requests = int(route.window_requests or 0) + 1
                route.window_failures = int(route.window_failures or 0) + 1
                route.consecutive_failures = int(route.consecutive_failures or 0) + 1
                route.last_failure_at = now
                route.last_error_code = _safe_error_code(error_code)
                should_open = (
                    route.health_status == "half_open"
                    or int(route.window_failures) >= int(route.failure_threshold)
                    or int(route.consecutive_failures) >= int(route.failure_threshold)
                )
                if should_open:
                    route.health_status = "open"
                    route.opened_at = now
                    route.cooldown_until = now + timedelta(seconds=int(route.cooldown_seconds))
                    route.half_open_claimed_until = None
            db.commit()
    except Exception:
        # Health accounting must never replace the provider result or failure.
        return


def route_id_from_model(model: Any) -> int | None:
    value = getattr(model, "route_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
