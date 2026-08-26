"""Read/seed runtime config (model ids, costs, defaults) from the DB."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import load_models_yaml, settings
from ..models import AppSetting, ModelConfig

DEFAULT_SETTINGS = {
    "reverse_prompt_enabled": True,
    "sms_auth_enabled": False,
    "payment_enabled": False,
    "navigation_states": {},
    "content_safety_enabled": False,
    "content_safety_banned_terms": "",
    "media_moderation_enabled": False,
    "media_moderation_fail_open": False,
    "image_n": 1,
    "image_size": "1024x1024",
    "asset_retention_days": settings.asset_retention_days,
    "audit_retention_days": 90,
    "admin_api_rate_per_hour": 600,
    "admin_quota_grant_single_limit": 100000,
    "admin_quota_grant_daily_limit": 500000,
    "review_task_sla_minutes": 30,
}


class ModelConfigResolutionError(ValueError):
    """The requested catalog entry cannot be used for the requested purpose."""


def get_model_config(db: Session, use: str) -> ModelConfig | None:
    return db.execute(
        select(ModelConfig)
        .where(ModelConfig.use == use, ModelConfig.deleted_at.is_(None))
        .order_by(ModelConfig.is_default.desc(), ModelConfig.sort_order, ModelConfig.id)
        .limit(1)
    ).scalar_one_or_none()


def resolve_model_config(
    db: Session,
    use: str,
    model_config_id: int | None = None,
    *,
    require_enabled: bool = True,
) -> ModelConfig:
    """Resolve a trusted catalog row instead of accepting runtime config from clients."""
    if model_config_id is not None:
        row = db.get(ModelConfig, int(model_config_id))
        if row is None or row.deleted_at is not None:
            raise ModelConfigResolutionError("所选模型不存在")
        if row.use != use:
            raise ModelConfigResolutionError(f"所选模型不支持 {use} 用途")
    else:
        row = db.execute(
            select(ModelConfig)
            .where(
                ModelConfig.use == use,
                ModelConfig.is_default.is_(True),
                ModelConfig.deleted_at.is_(None),
            )
            .order_by(ModelConfig.sort_order, ModelConfig.id)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise ModelConfigResolutionError(f"{use} 未配置默认模型")
    if require_enabled and not row.enabled:
        raise ModelConfigResolutionError("所选模型已停用")
    return row


def get_all_model_configs(db: Session) -> list[ModelConfig]:
    return list(
        db.execute(
            select(ModelConfig).where(ModelConfig.deleted_at.is_(None)).order_by(
                ModelConfig.use,
                ModelConfig.is_default.desc(),
                ModelConfig.sort_order,
                ModelConfig.id,
            )
        ).scalars()
    )


def get_setting(db: Session, key: str, default=None):
    row = db.get(AppSetting, key)
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    # values are stored as {"v": ...}
    return (row.value or {}).get("v", DEFAULT_SETTINGS.get(key, default))


def get_bool_setting(db: Session, key: str, default: bool = False) -> bool:
    value = get_setting(db, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(default)


def set_setting(db: Session, key: str, value) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value={"v": value})
        db.add(row)
    else:
        row.value = {"v": value}
    db.commit()


def set_settings(db: Session, values: dict) -> None:
    for key, value in values.items():
        row = db.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value={"v": value})
            db.add(row)
        else:
            row.value = {"v": value}
    db.commit()


def seed_from_yaml(db: Session) -> None:
    """Seed model_configs + defaults once, from models.yaml."""
    data = load_models_yaml()
    for m in data.get("models", []):
        use = m.get("use")
        if not use:
            continue
        existing = get_model_config(db, use)
        if existing:
            yaml_extra = m.get("extra")
            if yaml_extra:
                current_extra = dict(existing.extra or {})
                changed = False
                for key, value in dict(yaml_extra).items():
                    if key == "official_pricing" and current_extra.get(key) != value:
                        current_extra[key] = value
                        changed = True
                    if (
                        key == "capabilities"
                        and existing.model_id == m.get("model_id")
                        and "capabilities" not in current_extra
                    ):
                        current_extra[key] = value
                        changed = True
                if changed:
                    existing.extra = current_extra
            continue
        db.add(
            ModelConfig(
                use=use,
                model_id=m.get("model_id", ""),
                display_name=m.get("display_name") or m.get("model_id", ""),
                is_default=True,
                sort_order=int(m.get("sort_order", 0)),
                cost_credits=int(m.get("cost_credits", 1)),
                unlock_cost=int(m.get("unlock_cost", 0)),
                enabled=bool(m.get("enabled", True)),
                extra=m.get("extra"),
            )
        )
    db.commit()

    # Development/test databases use metadata.create_all instead of Alembic,
    # so seed the same immutable catalog versions production receives in 0044.
    from .model_versions import sync_model_versions

    for row in db.scalars(select(ModelConfig).order_by(ModelConfig.id)):
        sync_model_versions(db, row)
    db.commit()

    defaults = data.get("defaults", {})
    for key, val in {**DEFAULT_SETTINGS, **defaults}.items():
        if db.get(AppSetting, key) is None:
            db.add(AppSetting(key=key, value={"v": val}))
    db.commit()
