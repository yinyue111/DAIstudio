"""Read/seed runtime config (model ids, costs, defaults) from the DB."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import load_models_yaml
from ..models import AppSetting, ModelConfig

DEFAULT_SETTINGS = {
    "reverse_prompt_enabled": True,
    "image_n": 4,
    "image_size": "1024x1024",
    "asset_retention_days": 30,
    "audit_retention_days": 90,
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
