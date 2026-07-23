"""add immutable reverse revision lineage and source fingerprints

Revision ID: 0047_reverse_revision_lineage
Revises: 0046_generation_lineage
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0047_reverse_revision_lineage"
down_revision = "0046_generation_lineage"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {str(item.get("name") or "") for item in sa.inspect(op.get_bind()).get_indexes(table)}


def _checks(table: str) -> set[str]:
    return {
        str(item.get("name") or "")
        for item in sa.inspect(op.get_bind()).get_check_constraints(table)
    }


def _foreign_keys(table: str) -> set[str]:
    return {
        str(item.get("name") or "")
        for item in sa.inspect(op.get_bind()).get_foreign_keys(table)
    }


def upgrade() -> None:
    if "reverse_result_revisions" not in _tables():
        return

    table = "reverse_result_revisions"
    columns = _columns(table)
    checks = _checks(table)
    foreign_keys = _foreign_keys(table)
    with op.batch_alter_table(table) as batch:
        if "parent_revision_id" not in columns:
            batch.add_column(sa.Column("parent_revision_id", sa.BigInteger(), nullable=True))
        if "source_content_hash" not in columns:
            batch.add_column(sa.Column("source_content_hash", sa.String(length=64), nullable=True))
        if "source_fingerprints" not in columns:
            batch.add_column(sa.Column("source_fingerprints", sa.JSON(), nullable=True))
        if "payload_hash" not in columns:
            batch.add_column(sa.Column("payload_hash", sa.String(length=64), nullable=True))
        if "lineage_status" not in columns:
            batch.add_column(sa.Column(
                "lineage_status",
                sa.String(length=24),
                nullable=False,
                server_default="legacy_unverified",
            ))
        if "evidence_review_action" not in columns:
            batch.add_column(sa.Column("evidence_review_action", sa.String(length=16), nullable=True))
        if "fk_reverse_result_revisions_parent_revision_id" not in foreign_keys:
            batch.create_foreign_key(
                "fk_reverse_result_revisions_parent_revision_id",
                table,
                ["parent_revision_id"],
                ["id"],
            )
        if "ck_reverse_result_revisions_lineage_status_valid" not in checks:
            batch.create_check_constraint(
                "ck_reverse_result_revisions_lineage_status_valid",
                "lineage_status in ('verified', 'legacy_unverified')",
            )
        if "ck_reverse_result_revisions_evidence_action_valid" not in checks:
            batch.create_check_constraint(
                "ck_reverse_result_revisions_evidence_action_valid",
                "evidence_review_action IS NULL OR evidence_review_action in "
                "('not_applicable', 'inherited', 'updated', 'cleared')",
            )
        if "ck_reverse_result_revisions_parent_not_self" not in checks:
            batch.create_check_constraint(
                "ck_reverse_result_revisions_parent_not_self",
                "parent_revision_id IS NULL OR parent_revision_id <> id",
            )

    # Consecutive historical rows have a deterministic parent candidate, so link
    # them for read/audit purposes. They remain legacy_unverified because old rows
    # lack an immutable source-content hash and therefore cannot feed new jobs.
    op.get_bind().execute(sa.text(
        "UPDATE reverse_result_revisions SET parent_revision_id = ("
        "SELECT parent.id FROM reverse_result_revisions AS parent "
        "WHERE parent.operation_id = reverse_result_revisions.operation_id "
        "AND parent.user_id = reverse_result_revisions.user_id "
        "AND parent.version = reverse_result_revisions.version - 1 "
        "AND ((reverse_result_revisions.source = 'normalized' AND parent.source = 'provider_raw') "
        "OR (reverse_result_revisions.source = 'user_edit' AND parent.source = 'normalized') "
        "OR (reverse_result_revisions.source = 'applied' AND parent.source = 'user_edit') "
        "OR (reverse_result_revisions.source = 'model_compiled' AND parent.source = 'applied') "
        "OR (reverse_result_revisions.source = 'generation' AND parent.source = 'model_compiled'))"
        ") WHERE parent_revision_id IS NULL AND source <> 'provider_raw' "
        "AND EXISTS (SELECT 1 FROM reverse_result_revisions AS parent "
        "WHERE parent.operation_id = reverse_result_revisions.operation_id "
        "AND parent.user_id = reverse_result_revisions.user_id "
        "AND parent.version = reverse_result_revisions.version - 1 "
        "AND ((reverse_result_revisions.source = 'normalized' AND parent.source = 'provider_raw') "
        "OR (reverse_result_revisions.source = 'user_edit' AND parent.source = 'normalized') "
        "OR (reverse_result_revisions.source = 'applied' AND parent.source = 'user_edit') "
        "OR (reverse_result_revisions.source = 'model_compiled' AND parent.source = 'applied') "
        "OR (reverse_result_revisions.source = 'generation' AND parent.source = 'model_compiled')))"
    ))

    indexes = _indexes(table)
    if "ix_reverse_result_revisions_user_id" not in indexes:
        op.create_index(
            "ix_reverse_result_revisions_user_id",
            table,
            ["user_id"],
        )
    if "ix_reverse_result_revisions_parent_revision_id" not in indexes:
        op.create_index(
            "ix_reverse_result_revisions_parent_revision_id",
            table,
            ["parent_revision_id"],
        )
    if "ix_reverse_result_revisions_lineage_status" not in indexes:
        op.create_index(
            "ix_reverse_result_revisions_lineage_status",
            table,
            ["lineage_status"],
        )


def downgrade() -> None:
    if "reverse_result_revisions" not in _tables():
        return
    table = "reverse_result_revisions"
    indexes = _indexes(table)
    for name in (
        "ix_reverse_result_revisions_lineage_status",
        "ix_reverse_result_revisions_parent_revision_id",
        "ix_reverse_result_revisions_user_id",
    ):
        if name in indexes:
            op.drop_index(name, table_name=table)

    columns = _columns(table)
    checks = _checks(table)
    foreign_keys = _foreign_keys(table)
    with op.batch_alter_table(table) as batch:
        for name in (
            "ck_reverse_result_revisions_parent_not_self",
            "ck_reverse_result_revisions_evidence_action_valid",
            "ck_reverse_result_revisions_lineage_status_valid",
        ):
            if name in checks:
                batch.drop_constraint(name, type_="check")
        if "fk_reverse_result_revisions_parent_revision_id" in foreign_keys:
            batch.drop_constraint(
                "fk_reverse_result_revisions_parent_revision_id",
                type_="foreignkey",
            )
        for name in (
            "evidence_review_action",
            "lineage_status",
            "payload_hash",
            "source_fingerprints",
            "source_content_hash",
            "parent_revision_id",
        ):
            if name in columns:
                batch.drop_column(name)
