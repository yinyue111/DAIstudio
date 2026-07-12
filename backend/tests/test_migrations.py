import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

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
    admin_columns = {c["name"]: c for c in insp.get_columns("admin_idempotency_keys")}
    assert admin_columns["business_fingerprint"]["nullable"] is True
    assert admin_columns["fingerprint_expires_at"]["nullable"] is True
    admin_indexes = {idx["name"]: idx for idx in insp.get_indexes("admin_idempotency_keys")}
    business_index = admin_indexes["ix_admin_idempotency_business_window"]
    assert not business_index["unique"]
    assert business_index["column_names"] == [
        "admin_id",
        "scope",
        "business_fingerprint",
        "fingerprint_expires_at",
    ]


def test_0032_admin_quota_business_window_can_downgrade_and_reupgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "admin-quota-window.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "head")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    insp = sa.inspect(engine)
    assert "business_fingerprint" in {c["name"] for c in insp.get_columns("admin_idempotency_keys")}
    command.downgrade(cfg, "0031_jsonb_model_alignment")
    insp = sa.inspect(engine)
    assert "business_fingerprint" not in {c["name"] for c in insp.get_columns("admin_idempotency_keys")}
    command.upgrade(cfg, "head")
    insp = sa.inspect(engine)
    assert "business_fingerprint" in {c["name"] for c in insp.get_columns("admin_idempotency_keys")}
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


def test_reverse_recovery_migration_can_downgrade_and_reupgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "rollback-smoke.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "head")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    expected_audit_indexes = {
        "ix_audit_logs_user_id_id",
        "ix_audit_logs_action_id",
        "ix_audit_logs_created_at",
    }
    assert expected_audit_indexes <= {idx["name"] for idx in sa.inspect(engine).get_indexes("audit_logs")}
    insp = sa.inspect(engine)
    assert "reverse_operations" in insp.get_table_names()
    reverse_columns = {c["name"]: c for c in insp.get_columns("reverse_operations")}
    assert reverse_columns["client_request_id"]["nullable"] is True
    reverse_indexes = {idx["name"] for idx in insp.get_indexes("reverse_operations")}
    assert "ix_reverse_operations_status_updated" in reverse_indexes

    command.downgrade(cfg, "0029_default_image_n_one")
    insp = sa.inspect(engine)
    assert "reverse_operations" in insp.get_table_names()
    reverse_columns = {c["name"]: c for c in insp.get_columns("reverse_operations")}
    assert reverse_columns["client_request_id"]["nullable"] is False
    reverse_indexes = {idx["name"] for idx in insp.get_indexes("reverse_operations")}
    assert "ix_reverse_operations_status_updated" not in reverse_indexes
    assert expected_audit_indexes <= {idx["name"] for idx in insp.get_indexes("audit_logs")}

    command.upgrade(cfg, "head")

    insp = sa.inspect(engine)
    assert "user_prompts" in insp.get_table_names()
    assert "reverse_operations" in insp.get_table_names()
    reverse_columns = {c["name"]: c for c in insp.get_columns("reverse_operations")}
    assert reverse_columns["client_request_id"]["nullable"] is True
    reverse_indexes = {idx["name"] for idx in insp.get_indexes("reverse_operations")}
    assert "ix_reverse_operations_status_updated" in reverse_indexes
    assert expected_audit_indexes <= {idx["name"] for idx in insp.get_indexes("audit_logs")}
    gen_task_checks = {c["name"] for c in insp.get_check_constraints("gen_tasks")}
    assert "ck_gen_tasks_status_valid" in gen_task_checks


def test_0030_downgrade_blocks_when_anonymous_reverse_operations_exist(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-0030-anonymous-reverse.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "head")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "insert into users "
                "(id, phone, password_hash, balance_credits, frozen_credits, is_admin, status) "
                "values (1, '13900000997', 'hash', 0, 0, 0, 'active')"
            )
        )
        conn.execute(
            sa.text(
                "insert into reverse_operations "
                "(id, user_id, client_request_id, request_fingerprint, target, asset_url, status) "
                "values (1, 1, NULL, :fingerprint, 'image', 'https://cdn.example.com/a.jpg', 'failed')"
            ),
            {"fingerprint": "a" * 64},
        )

    with pytest.raises(RuntimeError, match="without client_request_id exist"):
        command.downgrade(cfg, "0029_default_image_n_one")


def _load_migration_module(revision: str):
    path = Path(__file__).resolve().parents[1] / f"alembic/versions/{revision}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    migration = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(migration)
    return migration


def test_0031_aligns_all_legacy_postgres_json_columns_with_jsonb_models(monkeypatch):
    migration = _load_migration_module("0031_jsonb_model_alignment")
    calls = []
    bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})()})()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda table, column, **kwargs: calls.append((table, column, kwargs)),
    )

    migration.upgrade()

    expected = {
        ("app_settings", "value"),
        ("audit_logs", "detail"),
        ("gateway_calls", "detail"),
        ("gen_tasks", "prompt"),
        ("gen_tasks", "params"),
        ("model_configs", "extra"),
        ("parse_records", "assets"),
        ("payment_orders", "raw"),
        ("payment_provider_configs", "public_config"),
        ("payment_provider_configs", "secret_config"),
    }
    assert {(table, column) for table, column, _ in calls} == expected
    assert all(isinstance(kwargs["existing_type"], postgresql.JSON) for _, _, kwargs in calls)
    assert all(isinstance(kwargs["type_"], postgresql.JSONB) for _, _, kwargs in calls)
    assert all(kwargs["postgresql_using"].endswith("::jsonb") for _, _, kwargs in calls)


def test_0031_is_a_noop_outside_postgresql(monkeypatch):
    migration = _load_migration_module("0031_jsonb_model_alignment")
    bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": "sqlite"})()})()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda *_args, **_kwargs: pytest.fail("SQLite must not receive PostgreSQL JSONB DDL"),
    )

    migration.upgrade()
    migration.downgrade()


def test_0022_downgrade_preserves_preexisting_team_package(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-0022-existing-team.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0021_parse_records_running")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    expected = {
        "id": "team",
        "title": "运营自定义团队包",
        "amount_cents": 45600,
        "credits": 6789,
        "badge": "限时运营",
        "enabled": 0,
        "sort_order": 77,
        "created_at": "2026-01-02 03:04:05",
        "updated_at": "2026-02-03 04:05:06",
    }
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO payment_packages
                    (id, title, amount_cents, credits, badge, enabled, sort_order,
                     created_at, updated_at)
                VALUES
                    (:id, :title, :amount_cents, :credits, :badge, :enabled, :sort_order,
                     :created_at, :updated_at)
                """
            ),
            expected,
        )

    command.upgrade(cfg, "0022_credit_pricing_defaults")
    command.downgrade(cfg, "0021_parse_records_running")

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                """
                SELECT id, title, amount_cents, credits, badge, enabled, sort_order,
                       created_at, updated_at
                FROM payment_packages
                WHERE id = 'team'
                """
            )
        ).mappings().one()
    assert dict(row) == expected


def test_0022_downgrade_preserves_operational_changes_to_seeded_team_package(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "migration-0022-modified-seeded-team.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0021_parse_records_running")
    command.upgrade(cfg, "0022_credit_pricing_defaults")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    expected = {
        "id": "team",
        "title": "运营调整团队包",
        "amount_cents": 51800,
        "credits": 8123,
        "badge": "运营专享",
        "enabled": 0,
        "sort_order": 88,
        "created_at": "2026-03-04 05:06:07",
        "updated_at": "2026-04-05 06:07:08",
    }
    with engine.begin() as conn:
        result = conn.execute(
            sa.text(
                """
                UPDATE payment_packages
                SET title = :title,
                    amount_cents = :amount_cents,
                    credits = :credits,
                    badge = :badge,
                    enabled = :enabled,
                    sort_order = :sort_order,
                    created_at = :created_at,
                    updated_at = :updated_at
                WHERE id = :id
                """
            ),
            expected,
        )
        assert result.rowcount == 1

    command.downgrade(cfg, "0021_parse_records_running")

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                """
                SELECT id, title, amount_cents, credits, badge, enabled, sort_order,
                       created_at, updated_at
                FROM payment_packages
                WHERE id = 'team'
                """
            )
        ).mappings().one()
    assert dict(row) == expected


def test_0022_empty_database_round_trip_keeps_one_usable_team_package(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-0022-empty-round-trip.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0021_parse_records_running")
    command.upgrade(cfg, "0022_credit_pricing_defaults")
    command.downgrade(cfg, "0021_parse_records_running")
    command.upgrade(cfg, "0022_credit_pricing_defaults")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                """
                SELECT id, title, amount_cents, credits, badge, enabled, sort_order,
                       created_at, updated_at
                FROM payment_packages
                WHERE id = 'team'
                """
            )
        ).mappings().all()

    assert len(rows) == 1
    team = rows[0]
    assert {
        key: team[key]
        for key in ("id", "title", "amount_cents", "credits", "badge", "enabled", "sort_order")
    } == {
        "id": "team",
        "title": "团队包",
        "amount_cents": 29900,
        "credits": 3800,
        "badge": "团队推荐",
        "enabled": 1,
        "sort_order": 40,
    }
    assert team["created_at"] is not None
    assert team["updated_at"] is not None


def test_0023_downgrade_blocks_when_canceled_tasks_exist(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-0023-canceled.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0023_events_cancel_prompt")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "insert into users "
                "(id, phone, password_hash, balance_credits, frozen_credits, is_admin, status) "
                "values (1, '13900000998', 'hash', 0, 0, 0, 'active')"
            )
        )
        conn.execute(
            sa.text(
                "insert into gen_tasks "
                "(id, user_id, category, stage, status, source_type, cost_frozen, cost_settled, params) "
                "values (1, 1, 'image', 'preview', 'canceled', 'image', 0, 0, '{}')"
            )
        )

    with pytest.raises(RuntimeError, match="canceled tasks exist"):
        command.downgrade(cfg, "0022_credit_pricing_defaults")


def test_0023_downgrade_removes_prompt_history_when_no_canceled_tasks(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-0023-clean.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0023_events_cancel_prompt")
    command.downgrade(cfg, "0022_credit_pricing_defaults")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    insp = sa.inspect(engine)
    assert "user_prompts" not in insp.get_table_names()


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


def test_0022_preserves_existing_sqlite_json_string_extra(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0022_credit_pricing_defaults.py"
    spec = importlib.util.spec_from_file_location("migration_0022", path)
    migration = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite:///:memory:")
    meta = sa.MetaData()
    model_configs = sa.Table(
        "model_configs",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("use", sa.String, unique=True),
        sa.Column("model_id", sa.String),
        sa.Column("cost_credits", sa.Integer),
        sa.Column("unlock_cost", sa.Integer),
        sa.Column("extra", sa.Text),
    )
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            model_configs.insert().values(
                use="video",
                model_id="custom-video",
                cost_credits=99,
                unlock_cost=7,
                extra='{"submit_path": "/custom/submit", "preview_cost": 5}',
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        migration._ensure_model_defaults("video", 16, unlock_cost=0, preview_cost=15)
        row = conn.execute(sa.text("SELECT cost_credits, unlock_cost, extra FROM model_configs")).first()
    assert row[0] == 99
    assert row[1] == 7
    assert row[2] == '{"submit_path": "/custom/submit", "preview_cost": 5}'


def test_0033_seeds_prompt_without_copying_vision_gateway(tmp_path, monkeypatch):
    db_path = tmp_path / "prompt-seed.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0032_admin_quota_business_window")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO model_configs (
                    use, model_id, provider, base_url, api_key_encrypted, gateway_format,
                    cost_credits, unlock_cost, enabled
                ) VALUES (
                    'vision', 'vision-model', 'openai', 'https://vision.example.com/v1',
                    'encrypted-vision-key', 'openai', 2, 0, 1
                )
                """
            )
        )

    command.upgrade(cfg, "0033_prompt_optimizer_model")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                """
                SELECT provider, base_url, api_key_encrypted, gateway_format
                FROM model_configs WHERE use = 'prompt'
                """
            )
        ).one()

    assert tuple(row) == (None, None, None, None)
