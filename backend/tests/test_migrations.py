import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

import app.models  # noqa: F401
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.config import settings
from app.db import Base


def test_alembic_revision_graph_is_linear_and_reaches_head():
    script = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "alembic"))
    heads = script.get_heads()
    assert len(heads) == 1
    revisions = list(script.walk_revisions(base="base", head=heads[0]))
    assert revisions
    assert all(len(rev._versioned_down_revisions) <= 1 for rev in revisions)


def test_migrated_schema_matches_model_nullability(tmp_path, monkeypatch):
    db_path = tmp_path / "migrated.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    insp = sa.inspect(engine)
    drift = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        if table_name not in insp.get_table_names():
            continue
        migrated_columns = {c["name"]: c for c in insp.get_columns(table_name)}
        for column in table.columns:
            if column.primary_key or column.name not in migrated_columns:
                continue
            actual_nullable = bool(migrated_columns[column.name]["nullable"])
            expected_nullable = bool(column.nullable)
            if actual_nullable != expected_nullable:
                drift.append(
                    f"{table_name}.{column.name}: migrated nullable="
                    f"{actual_nullable}, model nullable={expected_nullable}"
                )
    assert drift == []


def test_migrated_schema_has_core_integrity_constraints(tmp_path, monkeypatch):
    db_path = tmp_path / "integrity.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    insp = sa.inspect(engine)
    expected_checks = {
        "users": {"ck_users_balance_nonnegative", "ck_users_frozen_nonnegative"},
        "credit_transactions": {
            "ck_credit_transactions_type_valid",
            "ck_credit_transactions_balance_after_nonnegative",
            "ck_credit_transactions_frozen_after_nonnegative",
            "ck_credit_transactions_reserved_amount_nonnegative",
            "ck_credit_transactions_real_cost_nonnegative",
        },
        "gen_tasks": {
            "ck_gen_tasks_status_valid",
            "ck_gen_tasks_phase_valid",
            "ck_gen_tasks_cost_frozen_nonnegative",
            "ck_gen_tasks_cost_settled_nonnegative",
        },
        "model_configs": {
            "ck_model_configs_use_valid",
            "ck_model_configs_cost_credits_nonnegative",
            "ck_model_configs_unlock_cost_nonnegative",
        },
        "asset_reports": {
            "ck_asset_reports_status_valid",
            "ck_asset_reports_reason_valid",
        },
        "gen_assets": {
            "ck_gen_assets_moderation_status_valid",
        },
        "payment_orders": {
            "ck_payment_orders_amount_cents_positive",
            "ck_payment_orders_credits_positive",
            "ck_payment_orders_provider_valid",
            "ck_payment_orders_status_valid",
        },
        "payment_packages": {
            "ck_payment_packages_amount_cents_positive",
            "ck_payment_packages_credits_positive",
        },
        "payment_provider_configs": {
            "ck_payment_provider_configs_provider_valid",
            "ck_payment_provider_configs_mode_valid",
        },
    }
    for table, names in expected_checks.items():
        actual = {c["name"] for c in insp.get_check_constraints(table)}
        assert names <= actual
    indexes = {idx["name"] for idx in insp.get_indexes("credit_transactions")}
    assert "uq_credit_transactions_payment_grant" in indexes
    payment_indexes = {idx["name"] for idx in insp.get_indexes("payment_orders")}
    assert "uq_payment_orders_provider_trade_no" in payment_indexes
    credit_columns = {c["name"] for c in insp.get_columns("credit_transactions")}
    assert {
        "balance_delta",
        "frozen_delta",
        "frozen_after",
        "reserved_amount",
        "real_cost",
    } <= credit_columns
    model_config_indexes = {
        idx["name"]: idx for idx in insp.get_indexes("model_configs")
    }
    assert model_config_indexes["ix_model_configs_use"]["unique"]
    assert "asset_reports" in insp.get_table_names()
    asset_report_columns = {c["name"] for c in insp.get_columns("asset_reports")}
    assert {
        "asset_id",
        "reporter_user_id",
        "owner_user_id",
        "reason",
        "status",
        "handled_by",
        "handled_at",
    } <= asset_report_columns
    gen_asset_columns = {c["name"] for c in insp.get_columns("gen_assets")}
    assert "moderation_status" in gen_asset_columns
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "insert into payment_orders "
                    "(order_no, user_id, provider, package_id, amount_cents, credits, status) "
                    "values ('bad-pay', 1, 'paypal', 'starter', -1, 0, 'pending')"
                )
            )
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "insert into payment_provider_configs (provider, enabled, mode) "
                    "values ('wechat', 1, 'sandbox')"
                )
            )


def test_0009_reports_duplicate_active_final_tasks_before_index_creation():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0009_unique_active_final_tasks.py"
    spec = importlib.util.spec_from_file_location("migration_0009", path)
    migration = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    meta = sa.MetaData()
    gen_tasks = sa.Table(
        "gen_tasks",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer),
        sa.Column("parent_task_id", sa.Integer),
        sa.Column("category", sa.String),
        sa.Column("stage", sa.String),
        sa.Column("status", sa.String),
    )
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            gen_tasks.insert(),
            [
                {
                    "id": 101,
                    "user_id": 7,
                    "parent_task_id": 55,
                    "category": "video",
                    "stage": "final",
                    "status": "queued",
                },
                {
                    "id": 102,
                    "user_id": 7,
                    "parent_task_id": 55,
                    "category": "video",
                    "stage": "final",
                    "status": "needs_review",
                },
            ],
        )
        with pytest.raises(RuntimeError, match="task_ids=101,102"):
            migration._fail_on_duplicate_active_finals(conn, "sqlite")


def test_0011_reports_needs_review_duplicate_active_final_tasks_before_reindex():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0011_active_final_index_needs_review.py"
    spec = importlib.util.spec_from_file_location("migration_0011", path)
    migration = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    meta = sa.MetaData()
    gen_tasks = sa.Table(
        "gen_tasks",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer),
        sa.Column("parent_task_id", sa.Integer),
        sa.Column("category", sa.String),
        sa.Column("stage", sa.String),
        sa.Column("status", sa.String),
    )
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            gen_tasks.insert(),
            [
                {
                    "id": 201,
                    "user_id": 9,
                    "parent_task_id": 77,
                    "category": "video",
                    "stage": "final",
                    "status": "running",
                },
                {
                    "id": 202,
                    "user_id": 9,
                    "parent_task_id": 77,
                    "category": "video",
                    "stage": "final",
                    "status": "needs_review",
                },
            ],
        )
        with pytest.raises(RuntimeError, match="task_ids=201,202"):
            migration._fail_on_duplicate_active_finals(conn, "sqlite")
