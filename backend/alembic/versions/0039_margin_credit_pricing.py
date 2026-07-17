"""align packages and credits with the target gross margin

Revision ID: 0039_margin_credit_pricing
Revises: 0038_seedance_multi_reference
Create Date: 2026-07-17
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0039_margin_credit_pricing"
down_revision = "0038_seedance_multi_reference"
branch_labels = None
depends_on = None


PACKAGES = (
    {
        "id": "starter",
        "title": "体验包",
        "amount_cents": 2990,
        "credits": 300,
        "badge": None,
        "enabled": True,
        "sort_order": 10,
    },
    {
        "id": "creator",
        "title": "创作包",
        "amount_cents": 9900,
        "credits": 1050,
        "badge": "常用",
        "enabled": True,
        "sort_order": 20,
    },
    {
        "id": "pro",
        "title": "专业包",
        "amount_cents": 29900,
        "credits": 3300,
        "badge": "更划算",
        "enabled": True,
        "sort_order": 30,
    },
    {
        "id": "team",
        "title": "团队包",
        "amount_cents": 69900,
        "credits": 8000,
        "badge": "团队推荐",
        "enabled": True,
        "sort_order": 40,
    },
    {
        "id": "enterprise",
        "title": "企业包",
        "amount_cents": 99900,
        "credits": 12000,
        "badge": "最高优惠",
        "enabled": True,
        "sort_order": 50,
    },
)

MODEL_COSTS = {
    "prompt": 3,
    "vision": 5,
    "image": 8,
    "video": 100,
}


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _upsert_packages(bind, payment_packages: sa.Table) -> None:
    for package in PACKAGES:
        exists = bind.execute(
            sa.select(payment_packages.c.id).where(
                payment_packages.c.id == package["id"]
            )
        ).first()
        values = dict(package)
        if "updated_at" in payment_packages.c:
            values["updated_at"] = sa.func.now()
        if exists:
            bind.execute(
                sa.update(payment_packages)
                .where(payment_packages.c.id == package["id"])
                .values(**values)
            )
        else:
            bind.execute(sa.insert(payment_packages).values(**values))


def _update_models(bind, model_configs: sa.Table) -> None:
    for use, cost in MODEL_COSTS.items():
        values: dict = {"cost_credits": cost, "unlock_cost": 0}
        if "updated_at" in model_configs.c:
            values["updated_at"] = sa.func.now()
        bind.execute(
            sa.update(model_configs)
            .where(model_configs.c.use == use)
            .values(**values)
        )

    video_rows = bind.execute(
        sa.select(model_configs.c.id, model_configs.c.extra).where(
            model_configs.c.use == "video"
        )
    ).mappings()
    for row in video_rows:
        extra = _json_object(row["extra"])
        extra["preview_cost"] = 50
        values = {"extra": extra}
        if "updated_at" in model_configs.c:
            values["updated_at"] = sa.func.now()
        bind.execute(
            sa.update(model_configs)
            .where(model_configs.c.id == row["id"])
            .values(**values)
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    metadata = sa.MetaData()
    if "payment_packages" in tables:
        payment_packages = sa.Table(
            "payment_packages",
            metadata,
            autoload_with=bind,
        )
        _upsert_packages(bind, payment_packages)
    if "model_configs" in tables:
        model_configs = sa.Table(
            "model_configs",
            metadata,
            autoload_with=bind,
        )
        _update_models(bind, model_configs)


def downgrade() -> None:
    # Pricing may be edited by administrators after migration. Restoring stale
    # package or model values would silently overwrite those later decisions.
    pass
