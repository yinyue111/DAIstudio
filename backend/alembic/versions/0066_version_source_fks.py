"""repair immutable version source foreign keys

Revision ID: 0066_version_source_fks
Revises: 0065_reproduction_assessments
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0066_version_source_fks"
down_revision = "0065_reproduction_assessments"
branch_labels = None
depends_on = None

_VERSION_TABLES = (
    "model_capability_versions",
    "model_price_versions",
    "model_route_versions",
    "tool_versions",
)
_CONSTRAINT_NAMES = {
    "model_capability_versions": "fk_capability_versions_source",
    "model_price_versions": "fk_price_versions_source",
    "model_route_versions": "fk_route_versions_source",
    "tool_versions": "fk_tool_versions_source",
}


def _constraint_name(table: str) -> str:
    return _CONSTRAINT_NAMES[table]


def _source_foreign_keys(bind, table: str) -> list[dict]:
    return [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(table)
        if foreign_key.get("constrained_columns") == ["source_version_id"]
    ]


def _is_expected_source_foreign_key(foreign_key: dict, table: str) -> bool:
    options = foreign_key.get("options") or {}
    return (
        foreign_key.get("referred_table") == table
        and foreign_key.get("referred_columns") == ["id"]
        and str(options.get("ondelete") or "").upper() == "SET NULL"
    )


def _assert_no_orphan_sources(bind, table: str) -> None:
    orphan_count = bind.scalar(
        sa.text(
            f"SELECT count(*) FROM {table} child "
            f"LEFT JOIN {table} parent ON parent.id = child.source_version_id "
            "WHERE child.source_version_id IS NOT NULL AND parent.id IS NULL"
        )
    )
    if int(orphan_count or 0):
        raise RuntimeError(
            f"cannot repair {table}.source_version_id: {orphan_count} orphan rows"
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _VERSION_TABLES:
        columns = {column["name"] for column in inspector.get_columns(table)}
        if not {"id", "source_version_id"}.issubset(columns):
            raise RuntimeError(f"cannot repair {table}: version source columns are missing")
        existing = _source_foreign_keys(bind, table)
        if any(_is_expected_source_foreign_key(item, table) for item in existing):
            continue
        if existing:
            raise RuntimeError(
                f"cannot repair {table}.source_version_id: conflicting foreign key"
            )
        _assert_no_orphan_sources(bind, table)
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table) as batch_op:
                batch_op.create_foreign_key(
                    _constraint_name(table),
                    table,
                    ["source_version_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        else:
            op.create_foreign_key(
                _constraint_name(table),
                table,
                table,
                ["source_version_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    # These constraints are part of the 0063/0064 schema contract. This repair
    # revision only restores historical databases that missed the original DDL.
    pass
