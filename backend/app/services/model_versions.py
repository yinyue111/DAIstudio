"""Immutable capability and credit-price lifecycle for model catalog rows."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import ModelCapabilityVersion, ModelConfig, ModelPriceVersion
from .catalog_metadata import (
    MODEL_METADATA_FIELDS,
    MODEL_METADATA_SCHEMA_VERSION,
    model_metadata_snapshot,
    recorded_snapshot,
    snapshot_fields,
    snapshot_matches,
)
from .generation_pricing import snapshot_credit_pricing

ModelVersionKind = Literal["capability", "price"]
ModelVersionRow = ModelCapabilityVersion | ModelPriceVersion


def capability_snapshot(model: ModelConfig) -> dict:
    extra = model.extra if isinstance(model.extra, dict) else {}
    value = extra.get("capabilities")
    return deepcopy(value) if isinstance(value, dict) else {}


def price_snapshot(model: ModelConfig) -> dict:
    extra = model.extra if isinstance(model.extra, dict) else {}
    snapshot = snapshot_credit_pricing(extra)
    reverse_pricing = extra.get("reverse_pricing")
    if isinstance(reverse_pricing, dict):
        snapshot["reverse"] = deepcopy(reverse_pricing)
    return deepcopy(snapshot)


def normalize_price_version(pricing: dict) -> dict:
    """Resolve partial admin input into the same canonical table quotes consume."""
    raw = deepcopy(pricing or {})
    reverse_pricing = raw.pop("reverse", None)
    snapshot = snapshot_credit_pricing({"credit_pricing": raw})
    if isinstance(reverse_pricing, dict):
        snapshot["reverse"] = deepcopy(reverse_pricing)
    return deepcopy(snapshot)


def _version_model(kind: ModelVersionKind):
    return ModelCapabilityVersion if kind == "capability" else ModelPriceVersion


def _next_version(db: Session, version_model, model_config_id: int) -> int:
    current = db.scalar(
        select(func.max(version_model.version)).where(
            version_model.model_config_id == model_config_id
        )
    )
    return int(current or 0) + 1


def _lock_model(db: Session, model: ModelConfig) -> ModelConfig:
    if model.id is None:
        db.flush()
    return db.execute(
        select(ModelConfig)
        .where(ModelConfig.id == int(model.id))
        .with_for_update()
    ).scalar_one()


def model_version_row(
    db: Session,
    model_config_id: int,
    kind: ModelVersionKind,
    version: int,
    *,
    for_update: bool = False,
) -> ModelVersionRow | None:
    version_model = _version_model(kind)
    query = select(version_model).where(
        version_model.model_config_id == int(model_config_id),
        version_model.version == int(version),
    )
    if for_update:
        query = query.with_for_update()
    return db.scalar(query)


def _disable_row(row: ModelVersionRow, now: datetime) -> None:
    row.is_active = False
    row.status = "disabled"
    row.disabled_at = now


def _validated_model_metadata(row: ModelCapabilityVersion) -> dict:
    values = snapshot_fields(row.metadata_snapshot, MODEL_METADATA_FIELDS)
    if set(values) != set(MODEL_METADATA_FIELDS):
        raise ValueError("模型能力版本缺少完整目录元数据快照")
    if not isinstance(values["model_id"], str) or not values["model_id"].strip():
        raise ValueError("模型能力版本的模型标识无效")
    if not isinstance(values["display_name"], str) or not values["display_name"].strip():
        raise ValueError("模型能力版本的展示名称无效")
    if not isinstance(values["is_default"], bool) or not isinstance(values["enabled"], bool):
        raise ValueError("模型能力版本的启用或默认状态无效")
    if isinstance(values["sort_order"], bool) or not isinstance(values["sort_order"], int):
        raise ValueError("模型能力版本的排序无效")
    if values["is_default"] and not values["enabled"]:
        raise ValueError("默认模型的历史快照必须处于启用状态")
    return values


def _apply_model_default(
    db: Session,
    model: ModelConfig,
    *,
    is_default: bool,
    enabled: bool,
) -> None:
    list(
        db.scalars(
            select(ModelConfig.id)
            .where(ModelConfig.use == model.use)
            .with_for_update()
        )
    )
    if is_default:
        db.execute(
            update(ModelConfig)
            .where(ModelConfig.use == model.use, ModelConfig.id != model.id)
            .values(is_default=False)
        )
        model.is_default = True
        return

    current_default = db.scalar(
        select(ModelConfig).where(
            ModelConfig.use == model.use,
            ModelConfig.id != model.id,
            ModelConfig.is_default.is_(True),
            ModelConfig.enabled.is_(True),
        )
    )
    if current_default is None:
        current_default = db.scalar(
            select(ModelConfig)
            .where(
                ModelConfig.use == model.use,
                ModelConfig.id != model.id,
                ModelConfig.enabled.is_(True),
            )
            .order_by(ModelConfig.sort_order, ModelConfig.id)
            .limit(1)
        )
    if current_default is None:
        if enabled:
            raise ValueError("无法恢复非默认状态：该用途没有其他已启用模型")
        raise ValueError("无法恢复停用状态：该用途没有其他已启用默认模型")
    db.execute(
        update(ModelConfig)
        .where(ModelConfig.use == model.use, ModelConfig.id != current_default.id)
        .values(is_default=False)
    )
    current_default.is_default = True
    model.is_default = False


def _apply_capability_to_model(
    db: Session,
    model: ModelConfig,
    row: ModelCapabilityVersion,
) -> None:
    metadata = _validated_model_metadata(row)
    extra = deepcopy(model.extra or {})
    extra["capabilities"] = deepcopy(row.capabilities or {})
    model.extra = extra
    model.model_id = metadata["model_id"]
    model.display_name = metadata["display_name"]
    model.sort_order = metadata["sort_order"]
    model.enabled = metadata["enabled"]
    _apply_model_default(
        db,
        model,
        is_default=metadata["is_default"],
        enabled=metadata["enabled"],
    )


def _apply_price_to_model(model: ModelConfig, row: ModelPriceVersion) -> None:
    model.cost_credits = int(row.base_cost_credits or 0)
    model.unlock_cost = int(row.unlock_cost_credits or 0)
    pricing = deepcopy(row.pricing or {})
    reverse_pricing = pricing.pop("reverse", None)
    extra = deepcopy(model.extra or {})
    extra["credit_pricing"] = pricing
    if isinstance(reverse_pricing, dict):
        extra["reverse_pricing"] = reverse_pricing
    else:
        extra.pop("reverse_pricing", None)
    model.extra = extra


def _sync_sibling_model_versions(db: Session, model: ModelConfig) -> None:
    siblings = list(
        db.scalars(
            select(ModelConfig)
            .where(ModelConfig.use == model.use, ModelConfig.id != model.id)
            .order_by(ModelConfig.id)
            .with_for_update()
        )
    )
    for sibling in siblings:
        sync_model_versions(db, sibling)


def _activate_row(
    db: Session,
    model: ModelConfig,
    kind: ModelVersionKind,
    target: ModelVersionRow,
    now: datetime,
) -> ModelVersionRow:
    version_model = _version_model(kind)
    active = db.scalar(
        select(version_model).where(
            version_model.model_config_id == int(model.id),
            version_model.is_active.is_(True),
        )
    )
    if active is not None and int(active.id) != int(target.id):
        _disable_row(active, now)
        db.flush()

    target.status = "published"
    target.is_active = True
    target.activated_at = now
    target.disabled_at = None
    target.retired_at = None
    if kind == "capability":
        _apply_capability_to_model(db, model, target)
    else:
        _apply_price_to_model(model, target)
    db.flush()
    if kind == "capability":
        _sync_sibling_model_versions(db, model)
    return target


def create_model_version_draft(
    db: Session,
    model: ModelConfig,
    *,
    kind: ModelVersionKind,
    schema_version: str | None,
    capabilities: dict | None = None,
    base_cost_credits: int | None = None,
    unlock_cost_credits: int | None = None,
    pricing: dict | None = None,
) -> ModelVersionRow:
    """Append an editable draft without changing the runtime catalog."""
    model = _lock_model(db, model)
    version_model = _version_model(kind)
    common = {
        "model_config_id": int(model.id),
        "version": _next_version(db, version_model, int(model.id)),
        "status": "draft",
        "is_active": False,
        "activated_at": None,
    }
    if kind == "capability":
        if capabilities is None:
            raise ValueError("能力版本草稿缺少 capabilities")
        row = ModelCapabilityVersion(
            **common,
            schema_version=schema_version or "capability.v1",
            capabilities=deepcopy(capabilities),
            metadata_snapshot=model_metadata_snapshot(model),
        )
    else:
        if base_cost_credits is None or unlock_cost_credits is None or pricing is None:
            raise ValueError("价格版本草稿缺少价格字段")
        row = ModelPriceVersion(
            **common,
            schema_version=schema_version or "credit-price.v1",
            base_cost_credits=max(0, int(base_cost_credits)),
            unlock_cost_credits=max(0, int(unlock_cost_credits)),
            pricing=normalize_price_version(pricing),
        )
    db.add(row)
    db.flush()
    return row


def update_model_version_draft(
    db: Session,
    model: ModelConfig,
    *,
    kind: ModelVersionKind,
    version: int,
    values: dict,
) -> ModelVersionRow:
    """Edit a draft; published, disabled, and retired payloads stay immutable."""
    model = _lock_model(db, model)
    row = model_version_row(db, int(model.id), kind, version, for_update=True)
    if row is None:
        raise LookupError("模型版本不存在")
    if row.status != "draft":
        raise ValueError("只有草稿版本可以编辑")
    allowed = (
        {"schema_version", "capabilities"}
        if kind == "capability"
        else {
            "schema_version",
            "base_cost_credits",
            "unlock_cost_credits",
            "pricing",
        }
    )
    unexpected = set(values) - allowed
    if unexpected:
        raise ValueError("版本字段与类型不匹配: " + ",".join(sorted(unexpected)))
    for key, value in values.items():
        if value is None:
            raise ValueError(f"草稿字段 {key} 不能为空")
        if key == "pricing":
            value = normalize_price_version(value)
        elif key == "capabilities":
            value = deepcopy(value)
        elif key in {"base_cost_credits", "unlock_cost_credits"}:
            value = max(0, int(value))
        setattr(row, key, value)
    db.flush()
    return row


def publish_model_version(
    db: Session,
    model: ModelConfig,
    *,
    kind: ModelVersionKind,
    version: int,
) -> ModelVersionRow:
    model = _lock_model(db, model)
    target = model_version_row(db, int(model.id), kind, version, for_update=True)
    if target is None:
        raise LookupError("模型版本不存在")
    if target.status != "draft":
        raise ValueError("只有草稿版本可以发布")
    return _activate_row(db, model, kind, target, datetime.now(timezone.utc))


def disable_model_version(
    db: Session,
    model: ModelConfig,
    *,
    kind: ModelVersionKind,
    version: int,
) -> ModelVersionRow:
    """Disable the current version only after the owning model is off sale."""
    model = _lock_model(db, model)
    target = model_version_row(db, int(model.id), kind, version, for_update=True)
    if target is None:
        raise LookupError("模型版本不存在")
    if target.status == "disabled" and not target.is_active:
        return target
    if target.status != "published" or not target.is_active:
        raise ValueError("只有当前发布版本可以停用")
    if model.enabled:
        raise ValueError("请先在模型管理中停用该模型，再停用当前版本")
    _disable_row(target, datetime.now(timezone.utc))
    db.flush()
    return target


def retire_model_version(
    db: Session,
    model: ModelConfig,
    *,
    kind: ModelVersionKind,
    version: int,
) -> ModelVersionRow:
    """Permanently withdraw a non-active draft or disabled version."""
    model = _lock_model(db, model)
    target = model_version_row(db, int(model.id), kind, version, for_update=True)
    if target is None:
        raise LookupError("模型版本不存在")
    if target.is_active:
        raise ValueError("当前版本不能退役，请先发布或回滚到其他版本")
    if target.status == "retired":
        return target
    if target.status not in {"draft", "disabled"}:
        raise ValueError("只有草稿或已停用版本可以退役")
    target.status = "retired"
    target.is_active = False
    target.retired_at = datetime.now(timezone.utc)
    db.flush()
    return target


def rollback_model_version(
    db: Session,
    model: ModelConfig,
    *,
    kind: ModelVersionKind,
    version: int,
) -> ModelVersionRow:
    """Publish a copy of an old snapshot so historical version rows never mutate."""
    model = _lock_model(db, model)
    source = model_version_row(db, int(model.id), kind, version, for_update=True)
    if source is None:
        raise LookupError("模型版本不存在")
    if source.status != "disabled" or source.is_active:
        raise ValueError("只能回滚到已停用且未退役的历史版本")
    version_model = _version_model(kind)
    common = {
        "model_config_id": int(model.id),
        "version": _next_version(db, version_model, int(model.id)),
        "schema_version": source.schema_version,
        "source_version_id": int(source.id),
        "status": "published",
        "is_active": True,
        "activated_at": datetime.now(timezone.utc),
    }
    if kind == "capability":
        target = ModelCapabilityVersion(
            **common,
            capabilities=deepcopy(source.capabilities or {}),
            metadata_snapshot=recorded_snapshot(
                source.metadata_snapshot,
                schema_version=MODEL_METADATA_SCHEMA_VERSION,
                fields=MODEL_METADATA_FIELDS,
            ),
        )
    else:
        target = ModelPriceVersion(
            **common,
            base_cost_credits=int(source.base_cost_credits or 0),
            unlock_cost_credits=int(source.unlock_cost_credits or 0),
            pricing=deepcopy(source.pricing or {}),
        )
    active = db.scalar(
        select(version_model).where(
            version_model.model_config_id == int(model.id),
            version_model.is_active.is_(True),
        )
    )
    if active is not None:
        _disable_row(active, common["activated_at"])
        db.flush()
    db.add(target)
    db.flush()
    if kind == "capability":
        _apply_capability_to_model(db, model, target)
    else:
        _apply_price_to_model(model, target)
    db.flush()
    if kind == "capability":
        _sync_sibling_model_versions(db, model)
    return target


def sync_model_versions(
    db: Session,
    model: ModelConfig,
) -> tuple[ModelCapabilityVersion, ModelPriceVersion]:
    """Return active versions, publishing immutable rows for legacy catalog edits."""
    model = _lock_model(db, model)
    model_id = int(model.id)
    now = datetime.now(timezone.utc)

    expected_capabilities = capability_snapshot(model)
    expected_metadata = model_metadata_snapshot(model)
    capability = db.scalar(
        select(ModelCapabilityVersion).where(
            ModelCapabilityVersion.model_config_id == model_id,
            ModelCapabilityVersion.is_active.is_(True),
        )
    )
    if (
        capability is None
        or dict(capability.capabilities or {}) != expected_capabilities
        or not snapshot_matches(
            capability.metadata_snapshot,
            expected_metadata,
            fields=MODEL_METADATA_FIELDS,
        )
    ):
        if capability is not None:
            _disable_row(capability, now)
            db.flush()
        capability = ModelCapabilityVersion(
            model_config_id=model_id,
            version=_next_version(db, ModelCapabilityVersion, model_id),
            capabilities=expected_capabilities,
            metadata_snapshot=expected_metadata,
            status="published",
            is_active=True,
            activated_at=now,
        )
        db.add(capability)
        db.flush()

    expected_pricing = price_snapshot(model)
    base_cost = max(0, int(model.cost_credits or 0))
    unlock_cost = max(0, int(model.unlock_cost or 0))
    price = db.scalar(
        select(ModelPriceVersion).where(
            ModelPriceVersion.model_config_id == model_id,
            ModelPriceVersion.is_active.is_(True),
        )
    )
    price_matches = bool(
        price is not None
        and int(price.base_cost_credits or 0) == base_cost
        and int(price.unlock_cost_credits or 0) == unlock_cost
        and dict(price.pricing or {}) == expected_pricing
    )
    if not price_matches:
        if price is not None:
            _disable_row(price, now)
            db.flush()
        price = ModelPriceVersion(
            model_config_id=model_id,
            version=_next_version(db, ModelPriceVersion, model_id),
            base_cost_credits=base_cost,
            unlock_cost_credits=unlock_cost,
            pricing=expected_pricing,
            status="published",
            is_active=True,
            activated_at=now,
        )
        db.add(price)
        db.flush()

    return capability, price
