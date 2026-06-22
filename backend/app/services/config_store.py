"""Read/seed runtime config (model ids, costs, defaults) from the DB."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import load_models_yaml
from ..models import AppSetting, ModelConfig

DEFAULT_SETTINGS = {
    "reverse_prompt_enabled": True,
    "sms_auth_enabled": False,
    "payment_enabled": False,
    "content_safety_enabled": False,
    "content_safety_banned_terms": "",
    "image_n": 4,
    "image_size": "1024x1024",
    "asset_retention_days": 30,
    "audit_retention_days": 90,
    "admin_api_rate_per_hour": 600,
    "admin_quota_grant_single_limit": 100000,
    "admin_quota_grant_daily_limit": 500000,
    "review_task_sla_minutes": 30,
}


def get_model_config(db: Session, use: str) -> ModelConfig | None:
    return db.execute(
        select(ModelConfig).where(ModelConfig.use == use)
    ).scalar_one_or_none()


def get_all_model_configs(db: Session) -> list[ModelConfig]:
    return list(db.execute(select(ModelConfig).order_by(ModelConfig.use)).scalars())


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
                if changed:
                    existing.extra = current_extra
            continue
        db.add(
            ModelConfig(
                use=use,
                model_id=m.get("model_id", ""),
                cost_credits=int(m.get("cost_credits", 1)),
                unlock_cost=int(m.get("unlock_cost", 0)),
                enabled=bool(m.get("enabled", True)),
                extra=m.get("extra"),
            )
        )
    db.commit()

    defaults = data.get("defaults", {})
    for key, val in {**DEFAULT_SETTINGS, **defaults}.items():
        if db.get(AppSetting, key) is None:
            db.add(AppSetting(key=key, value={"v": val}))
    db.commit()
