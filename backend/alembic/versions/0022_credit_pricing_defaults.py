"""update default credit pricing packages

Revision ID: 0022_credit_pricing_defaults
Revises: 0021_parse_records_running
Create Date: 2026-06-26
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0022_credit_pricing_defaults"
down_revision = "0021_parse_records_running"
branch_labels = None
depends_on = None


PACKAGES = [
    ("starter", "体验包", 990, 100, None, True, 10),
    ("creator", "创作包", 2990, 330, "常用", True, 20),
    ("pro", "专业包", 9990, 1200, "更划算", True, 30),
    ("team", "团队包", 29900, 3800, "团队推荐", True, 40),
]


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _quote(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


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


def _upsert_package(package_id: str, title: str, amount_cents: int, credits: int,
                    badge: str | None, enabled: bool, sort_order: int) -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT 1 FROM payment_packages WHERE id = :id"),
        {"id": package_id},
    ).first()
    if exists:
        return
    bind.execute(
        sa.text(
            """
            INSERT INTO payment_packages
                (id, title, amount_cents, credits, badge, enabled, sort_order)
            VALUES
                (:id, :title, :amount_cents, :credits, :badge, :enabled, :sort_order)
            """
        ),
        {
            "id": package_id,
            "title": title,
            "amount_cents": amount_cents,
            "credits": credits,
            "badge": badge,
            "enabled": enabled,
            "sort_order": sort_order,
        },
    )


def _ensure_model_defaults(use: str, cost_credits: int, unlock_cost: int | None = None, preview_cost: int | None = None) -> None:
    if "model_configs" not in _tables():
        return
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            f"SELECT cost_credits, unlock_cost, extra FROM {_quote('model_configs')} "
            f"WHERE {_quote('use')} = :use"
        ),
        {"use": use},
    ).first()
    if not row:
        return
    updates: list[str] = []
    values = {"use": use, "cost": cost_credits, "unlock": unlock_cost}
    if row[0] is None:
        updates.append("cost_credits = :cost")
    if unlock_cost is not None and row[1] is None:
        updates.append("unlock_cost = :unlock")
    if updates:
        bind.execute(
            sa.text(
                f"UPDATE {_quote('model_configs')} SET {', '.join(updates)} "
                f"WHERE {_quote('use')} = :use"
            ),
            values,
        )
    if preview_cost is not None:
        raw_extra = row[2] if row else None
        extra = _json_object(raw_extra)
        if "preview_cost" in extra:
            return
        extra["preview_cost"] = preview_cost
        if bind.dialect.name == "postgresql":
            bind.execute(
                sa.text(
                    f"""
                    UPDATE {_quote('model_configs')}
                    SET extra = CAST(:extra AS jsonb)
                    WHERE {_quote('use')} = :use
                    """
                ),
                {"use": use, "extra": json.dumps(extra, ensure_ascii=False)},
            )
            return
        bind.execute(
            sa.text(f"UPDATE {_quote('model_configs')} SET extra = :extra WHERE {_quote('use')} = :use"),
            {"use": use, "extra": json.dumps(extra, ensure_ascii=False)},
        )


def upgrade() -> None:
    if "payment_packages" in _tables():
        for item in PACKAGES:
            _upsert_package(*item)
    _ensure_model_defaults("vision", 2)
    _ensure_model_defaults("image", 15, unlock_cost=0)
    _ensure_model_defaults("video", 16, unlock_cost=0, preview_cost=15)


def downgrade() -> None:
    # The upgrade cannot distinguish a seeded team package from preexisting data.
    pass
