"""core integrity constraints and payment ledger idempotency

Revision ID: 0016_core_integrity_constraints
Revises: 0015_tighten_not_null
Create Date: 2026-06-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0016_core_integrity_constraints"
down_revision = "0015_tighten_not_null"
branch_labels = None
depends_on = None


TABLE_CHECKS = {
    "users": [
        ("ck_users_status_valid", "status in ('active', 'pending', 'disabled')"),
        ("ck_users_balance_nonnegative", "balance_credits >= 0"),
        ("ck_users_frozen_nonnegative", "frozen_credits >= 0"),
    ],
    "credit_transactions": [
        (
            "ck_credit_transactions_type_valid",
            "type in ('grant', 'freeze', 'settle', 'refund', 'unlock', 'consume')",
        ),
        ("ck_credit_transactions_balance_after_nonnegative", "balance_after >= 0"),
    ],
    "uploaded_assets": [
        ("ck_uploaded_assets_bytes_nonnegative", "bytes IS NULL OR bytes >= 0"),
    ],
    "parse_records": [
        ("ck_parse_records_status_valid", "status in ('queued', 'done', 'failed')"),
    ],
    "gen_tasks": [
        (
            "ck_gen_tasks_source_type_valid",
            "source_type IS NULL OR source_type in ('image', 'video')",
        ),
        ("ck_gen_tasks_category_valid", "category in ('image', 'video')"),
        ("ck_gen_tasks_stage_valid", "stage in ('preview', 'final')"),
        (
            "ck_gen_tasks_status_valid",
            "status in ('queued', 'running', 'succeeded', 'failed', 'needs_review')",
        ),
        (
            "ck_gen_tasks_phase_valid",
            "phase IS NULL OR phase in ('submitting', 'polling', 'downloading', 'reconciling')",
        ),
        ("ck_gen_tasks_cost_frozen_nonnegative", "cost_frozen >= 0"),
        ("ck_gen_tasks_cost_settled_nonnegative", "cost_settled >= 0"),
    ],
    "gen_assets": [
        ("ck_gen_assets_type_valid", "type in ('image', 'video')"),
        ("ck_gen_assets_width_positive", "width IS NULL OR width > 0"),
        ("ck_gen_assets_height_positive", "height IS NULL OR height > 0"),
        ("ck_gen_assets_duration_nonnegative", "duration IS NULL OR duration >= 0"),
    ],
    "gateway_calls": [
        (
            "ck_gateway_calls_kind_valid",
            "kind in ('reverse', 'image', 'video_submit', 'video_poll', 'video_download')",
        ),
        ("ck_gateway_calls_status_valid", "status IS NULL OR status in ('ok', 'failed')"),
        ("ck_gateway_calls_latency_nonnegative", "latency_ms IS NULL OR latency_ms >= 0"),
        (
            "ck_gateway_calls_prompt_tokens_nonnegative",
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
        ),
        (
            "ck_gateway_calls_completion_tokens_nonnegative",
            "completion_tokens IS NULL OR completion_tokens >= 0",
        ),
        (
            "ck_gateway_calls_total_tokens_nonnegative",
            "total_tokens IS NULL OR total_tokens >= 0",
        ),
    ],
    "model_configs": [
        ("ck_model_configs_use_valid", "use in ('vision', 'image', 'video')"),
        (
            "ck_model_configs_gateway_format_valid",
            "gateway_format IS NULL OR gateway_format in ('openai', 'ark')",
        ),
        ("ck_model_configs_cost_credits_nonnegative", "cost_credits >= 0"),
        ("ck_model_configs_unlock_cost_nonnegative", "unlock_cost >= 0"),
    ],
}


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _quote(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


def _check_names(table_name: str) -> set[str]:
    try:
        return {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table_name)}
    except NotImplementedError:
        return set()


def _index_names(table_name: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _preflight_checks() -> None:
    bind = op.get_bind()
    tables = _existing_tables()
    for table_name, checks in TABLE_CHECKS.items():
        if table_name not in tables:
            continue
        for name, condition in checks:
            count = bind.execute(
                sa.text(
                    f"SELECT COUNT(*) FROM {_quote(table_name)} "
                    f"WHERE NOT ({condition})"
                )
            ).scalar()
            if count:
                raise RuntimeError(
                    f"0016_core_integrity_constraints blocked: "
                    f"{name} would fail for {count} existing row(s)"
                )


def _preflight_payment_grant_duplicates() -> None:
    if "credit_transactions" not in _existing_tables():
        return
    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT biz_ref, COUNT(*) AS n
            FROM credit_transactions
            WHERE biz_type = 'payment'
              AND type = 'grant'
              AND biz_ref IS NOT NULL
            GROUP BY biz_ref
            HAVING COUNT(*) > 1
            LIMIT 5
            """
        )
    ).mappings().all()
    if rows:
        sample = ", ".join(f"biz_ref={r['biz_ref']} n={r['n']}" for r in rows)
        raise RuntimeError(
            "0016_core_integrity_constraints blocked: duplicate payment grants "
            f"exist ({sample})"
        )


def _create_checks() -> None:
    tables = _existing_tables()
    for table_name, checks in TABLE_CHECKS.items():
        if table_name not in tables:
            continue
        existing = _check_names(table_name)
        with op.batch_alter_table(table_name) as batch:
            for name, condition in checks:
                if name in existing:
                    continue
                batch.create_check_constraint(name, condition)


def _drop_checks() -> None:
    tables = _existing_tables()
    for table_name, checks in reversed(TABLE_CHECKS.items()):
        if table_name not in tables:
            continue
        existing = _check_names(table_name)
        with op.batch_alter_table(table_name) as batch:
            for name, _condition in reversed(checks):
                if name not in existing:
                    continue
                batch.drop_constraint(name, type_="check")


def upgrade() -> None:
    _preflight_checks()
    _preflight_payment_grant_duplicates()
    _create_checks()
    if (
        "credit_transactions" in _existing_tables()
        and "uq_credit_transactions_payment_grant" not in _index_names("credit_transactions")
    ):
        op.create_index(
            "uq_credit_transactions_payment_grant",
            "credit_transactions",
            ["biz_type", "biz_ref", "type"],
            unique=True,
            postgresql_where=sa.text("biz_type = 'payment' AND type = 'grant' AND biz_ref IS NOT NULL"),
            sqlite_where=sa.text("biz_type = 'payment' AND type = 'grant' AND biz_ref IS NOT NULL"),
        )


def downgrade() -> None:
    if (
        "credit_transactions" in _existing_tables()
        and "uq_credit_transactions_payment_grant" in _index_names("credit_transactions")
    ):
        op.drop_index("uq_credit_transactions_payment_grant", table_name="credit_transactions")
    _drop_checks()
