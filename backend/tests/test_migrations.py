import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
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
from app.workflow_schemas import WorkflowSpec


def test_alembic_revision_graph_is_linear_and_reaches_head():
    script = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "alembic"))
    heads = script.get_heads()
    assert len(heads) == 1
    revisions = list(script.walk_revisions(base="base", head=heads[0]))
    assert revisions
    assert all(len(rev.revision) <= 32 for rev in revisions)
    assert all(len(rev._versioned_down_revisions) <= 1 for rev in revisions)


def test_postgresql_constraint_names_fit_identifier_limit():
    names = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if constraint.name
    }
    assert all(len(name) <= 63 for name in names)


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
        "reverse_operations": {
            "ck_reverse_operations_status_valid",
            "ck_reverse_operations_progress_range",
            "ck_reverse_operations_cost_frozen_nonnegative",
            "ck_reverse_operations_cost_settled_nonnegative",
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


def test_0060_0061_execution_quotes_round_trip(tmp_path, monkeypatch):
    db_path = tmp_path / "execution-quotes-round-trip.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0059_recipe_usage_context_jsonb")
    command.upgrade(cfg, "0061_paid_action_quotes")
    engine = sa.create_engine(database_url)
    upgraded = sa.inspect(engine)
    quote_columns = {row["name"] for row in upgraded.get_columns("generation_quotes")}
    proposal_columns = {
        row["name"] for row in upgraded.get_columns("prompt_optimization_proposals")
    }
    assert {
        "kind",
        "client_request_id",
        "subject_snapshot",
        "warnings",
        "consumed_ref_type",
        "consumed_ref_id",
    } <= quote_columns
    assert "quote_id" in proposal_columns
    kind_check = next(
        row for row in upgraded.get_check_constraints("generation_quotes")
        if row["name"] == "ck_generation_quotes_kind_valid"
    )
    assert "prompt_optimization" in kind_check["sqltext"]
    assert "asset_unlock" in kind_check["sqltext"]

    command.downgrade(cfg, "0059_recipe_usage_context_jsonb")
    downgraded = sa.inspect(engine)
    assert "kind" not in {
        row["name"] for row in downgraded.get_columns("generation_quotes")
    }
    assert "quote_id" not in {
        row["name"] for row in downgraded.get_columns("prompt_optimization_proposals")
    }

    command.upgrade(cfg, "0061_paid_action_quotes")
    reupgraded = sa.inspect(engine)
    assert "kind" in {
        row["name"] for row in reupgraded.get_columns("generation_quotes")
    }
    assert "quote_id" in {
        row["name"] for row in reupgraded.get_columns("prompt_optimization_proposals")
    }


def test_0062_gen_assets_task_nullable_round_trip_and_safe_downgrade(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "gen-assets-task-nullable.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0061_paid_action_quotes")
    engine = sa.create_engine(database_url)
    assert {
        column["name"]: column for column in sa.inspect(engine).get_columns("gen_assets")
    }["task_id"]["nullable"] is False

    command.upgrade(cfg, "0062_gen_assets_task_nullable")
    assert {
        column["name"]: column for column in sa.inspect(engine).get_columns("gen_assets")
    }["task_id"]["nullable"] is True
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users "
                "(id, phone, status, is_admin, balance_credits, frozen_credits) "
                "VALUES (6201, '13976200001', 'active', 0, 0, 0)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO gen_assets "
                "(id, user_id, type, watermarked, unlocked, moderation_status) "
                "VALUES (6202, 6201, 'image', 0, 0, 'active')"
            )
        )

    with pytest.raises(RuntimeError, match=r"asset_ids=6202"):
        command.downgrade(cfg, "0061_paid_action_quotes")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0062_gen_assets_task_nullable"
        )

    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM gen_assets WHERE id = 6202"))
    command.downgrade(cfg, "0061_paid_action_quotes")
    assert {
        column["name"]: column for column in sa.inspect(engine).get_columns("gen_assets")
    }["task_id"]["nullable"] is False

    command.upgrade(cfg, "0062_gen_assets_task_nullable")
    assert {
        column["name"]: column for column in sa.inspect(engine).get_columns("gen_assets")
    }["task_id"]["nullable"] is True
    engine.dispose()


def test_0063_model_version_lifecycle_round_trip_and_backfill(tmp_path, monkeypatch):
    db_path = tmp_path / "model-version-lifecycle.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0062_gen_assets_task_nullable")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO model_configs "
                "(id, use, model_id, display_name, is_default, sort_order, "
                "cost_credits, unlock_cost, enabled, extra) "
                "VALUES (6301, 'image', 'lifecycle-migration-test', "
                "'Lifecycle Migration Test', 0, 6301, 7, 2, 1, '{}')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO model_capability_versions "
                "(id, model_config_id, version, schema_version, capabilities, "
                "is_active, activated_at, retired_at) VALUES "
                "(6311, 6301, 1, 'capability.v1', '{}', 1, CURRENT_TIMESTAMP, NULL), "
                "(6312, 6301, 2, 'capability.v1', '{}', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO model_price_versions "
                "(id, model_config_id, version, schema_version, base_cost_credits, "
                "unlock_cost_credits, pricing, is_active, activated_at, retired_at) VALUES "
                "(6321, 6301, 1, 'credit-price.v1', 7, 2, '{}', 1, CURRENT_TIMESTAMP, NULL), "
                "(6322, 6301, 2, 'credit-price.v1', 8, 3, '{}', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(cfg, "0063_model_version_lifecycle")
    upgraded = sa.inspect(engine)
    for table in ("model_capability_versions", "model_price_versions"):
        columns = {column["name"]: column for column in upgraded.get_columns(table)}
        assert {"status", "source_version_id", "disabled_at", "updated_at"} <= set(
            columns
        )
        assert columns["activated_at"]["nullable"] is True
        checks = {item["name"] for item in upgraded.get_check_constraints(table)}
        assert f"ck_{table}_status_valid" in checks
        assert f"ck_{table}_active_status" in checks
        indexes = {item["name"] for item in upgraded.get_indexes(table)}
        assert f"ix_{table}_source_version_id" in indexes
        source_fks = [
            item
            for item in upgraded.get_foreign_keys(table)
            if item["constrained_columns"] == ["source_version_id"]
        ]
        assert len(source_fks) == 1
        assert source_fks[0]["referred_table"] == table
        assert source_fks[0]["referred_columns"] == ["id"]

    with engine.begin() as connection:
        capability_rows = connection.execute(
            sa.text(
                "SELECT id, status, disabled_at, retired_at "
                "FROM model_capability_versions WHERE model_config_id = 6301 ORDER BY version"
            )
        ).mappings().all()
        assert capability_rows[0]["status"] == "published"
        assert capability_rows[1]["status"] == "disabled"
        assert capability_rows[1]["disabled_at"] is not None
        assert capability_rows[1]["retired_at"] is None
        connection.execute(
            sa.text(
                "INSERT INTO model_capability_versions "
                "(id, model_config_id, version, schema_version, capabilities, status, "
                "is_active, activated_at) VALUES "
                "(6313, 6301, 3, 'capability.v2', '{}', 'draft', 0, NULL)"
            )
        )

    command.downgrade(cfg, "0062_gen_assets_task_nullable")
    downgraded = sa.inspect(engine)
    capability_columns = {
        column["name"]: column
        for column in downgraded.get_columns("model_capability_versions")
    }
    assert "status" not in capability_columns
    assert capability_columns["activated_at"]["nullable"] is False
    with engine.connect() as connection:
        draft_after_downgrade = connection.execute(
            sa.text(
                "SELECT activated_at, retired_at FROM model_capability_versions WHERE id = 6313"
            )
        ).mappings().one()
        assert draft_after_downgrade["activated_at"] is not None
        assert draft_after_downgrade["retired_at"] is not None

    command.upgrade(cfg, "0063_model_version_lifecycle")
    assert "status" in {
        column["name"]
        for column in sa.inspect(engine).get_columns("model_capability_versions")
    }
    engine.dispose()


def test_0064_immutable_runtime_versions_backfills_compiled_workflow_snapshot(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "immutable-runtime-versions.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0063_model_version_lifecycle")
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(bind=engine)
    tables = metadata.tables
    workflow = {
        "type": "workflow.v1",
        "output_node": "export",
        "studio_preset": {"creation_mode": "video_edit"},
        "nodes": [
            {
                "key": "parse",
                "type": "parse",
                "depends_on": [],
                "config": {"safety_policy": {"owner_required": True}},
                "max_attempts": 2,
                "side_effect": False,
            },
            {
                "key": "export",
                "type": "export",
                "depends_on": ["parse"],
                "config": {"format": "zip"},
                "max_attempts": 1,
                "side_effect": True,
                "compensation": {"type": "delete_export", "config": {}},
            },
        ],
    }
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            tables["users"].insert().values(
                id=6401,
                phone="13976400001",
                status="active",
                is_admin=False,
                balance_credits=100,
                frozen_credits=7,
            )
        )
        connection.execute(
            tables["tool_definitions"].insert().values(
                id=6402,
                slug="legacy-export",
                name="Legacy export",
                description="legacy migration fixture",
                category="workflow",
                renderer="studio",
                entry_path="/?tool=legacy-export",
                sort_order=4,
                enabled=True,
                featured=True,
            )
        )
        connection.execute(
            tables["tool_versions"].insert().values(
                id=6403,
                tool_definition_id=6402,
                version=1,
                schema_version="tool.v1",
                input_schema={"type": "object"},
                workflow=workflow,
                pricing_policy={"fixed_credits": 7},
                capabilities={"safety_policy": {"content": "strict"}},
                is_active=True,
            )
        )
        connection.execute(
            tables["generation_quotes"].insert().values(
                id=6404,
                user_id=6401,
                kind="workflow",
                client_request_id="legacy-workflow-quote",
                tool_version_id=6403,
                request_fingerprint="a" * 64,
                category="workflow",
                stage="preview",
                request_snapshot={"project_id": 6407, "input": {"topic": "legacy"}},
                model_snapshot={
                    "model_config_id": 6410,
                    "route_snapshot": {"route_id": 6408, "route_version_id": 6409},
                },
                pricing_snapshot={"fixed_credits": 7},
                price_breakdown={"items": [{"key": "workflow", "credits": 7}]},
                subject_snapshot={"tool_slug": "legacy-export"},
                warnings=["legacy warning"],
                estimated_credits=7,
                status="active",
                expires_at=now + timedelta(hours=1),
            )
        )
        connection.execute(
            tables["tool_runs"].insert().values(
                id=6405,
                user_id=6401,
                tool_definition_id=6402,
                tool_version_id=6403,
                quote_id=6404,
                client_request_id="legacy-workflow-run",
                request_fingerprint="b" * 64,
                status="queued",
                input_snapshot={"topic": "legacy"},
                pricing_snapshot={"fixed_credits": 7},
                cost_frozen=7,
                cost_settled=0,
            )
        )
        connection.execute(
            tables["workflow_runs"].insert().values(
                id=6406,
                tool_run_id=6405,
                user_id=6401,
                workflow_schema_version="workflow.v1",
                workflow_snapshot=workflow,
                status="queued",
                revision=0,
            )
        )
        connection.execute(
            tables["media_projects"].insert().values(
                id=6407,
                user_id=6401,
                title="Legacy workflow project",
                project_type="video",
                status="active",
            )
        )
        connection.execute(
            tables["media_project_tasks"].insert().values(
                project_id=6407,
                task_kind="workflow",
                task_id=6406,
            )
        )

    def assert_upgraded() -> None:
        inspector = sa.inspect(engine)
        columns = {row["name"]: row for row in inspector.get_columns("workflow_runs")}
        assert {"compiled_snapshot", "compiled_snapshot_hash", "compiled_at"} <= set(
            columns
        )
        assert columns["compiled_snapshot"]["nullable"] is False
        assert columns["compiled_snapshot_hash"]["nullable"] is False
        assert columns["compiled_snapshot_hash"]["default"] is None
        assert columns["compiled_at"]["nullable"] is False
        checks = {
            row["name"] for row in inspector.get_check_constraints("workflow_runs")
        }
        assert "ck_workflow_runs_compiled_snapshot_hash_length" in checks
        for table in ("model_route_versions", "tool_versions"):
            source_fks = [
                item
                for item in inspector.get_foreign_keys(table)
                if item["constrained_columns"] == ["source_version_id"]
            ]
            assert len(source_fks) == 1
            assert source_fks[0]["referred_table"] == table
            assert source_fks[0]["referred_columns"] == ["id"]
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT compiled_snapshot, compiled_snapshot_hash, compiled_at "
                    "FROM workflow_runs WHERE id = 6406"
                )
            ).mappings().one()
            snapshot = json.loads(row["compiled_snapshot"])
            current = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert current == "0064_immutable_runtime_versions"
        assert row["compiled_at"] is not None
        assert snapshot["schema_version"] == "workflow-compiled.v1"
        assert snapshot["legacy_backfill"] is True
        assert "node_graph" not in snapshot
        assert snapshot["tool"]["definition"]["id"] == 6402
        assert snapshot["tool"]["version"]["id"] == 6403
        assert snapshot["input"] == {
            "project_id": 6407,
            "client_request_id": "legacy-workflow-run",
            "request_fingerprint": "b" * 64,
            "payload": {"topic": "legacy"},
        }
        assert snapshot["dag"]["declared_order"] == ["parse", "export"]
        assert snapshot["dag"]["topological_order"] == ["parse", "export"]
        assert snapshot["quote_binding"]["id"] == 6404
        assert snapshot["pricing_binding"]["estimated_credits"] == 7
        assert snapshot["runtime_references"]["route_ids"] == [6408]
        assert snapshot["runtime_references"]["route_version_ids"] == [6409]
        assert snapshot["retry_policy"]["nodes"] == [
            {"key": "parse", "max_attempts": 2},
            {"key": "export", "max_attempts": 1},
        ]
        assert snapshot["failure_policy"]["refund_unsettled_reservation"] is True
        assert snapshot["compensation_policy"]["nodes"] == [
            {
                "key": "export",
                "side_effect": True,
                "compensation": {"type": "delete_export", "config": {}},
            }
        ]
        canonical = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert row["compiled_snapshot_hash"] == hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        assert len(row["compiled_snapshot_hash"]) == 64

    command.upgrade(cfg, "0064_immutable_runtime_versions")
    assert_upgraded()
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE workflow_runs SET compiled_snapshot_hash = :digest WHERE id = 6406"
            ),
            {"digest": "x" * 63},
        )

    command.downgrade(cfg, "0063_model_version_lifecycle")
    downgraded = sa.inspect(engine)
    assert {
        "compiled_snapshot",
        "compiled_snapshot_hash",
        "compiled_at",
    }.isdisjoint({row["name"] for row in downgraded.get_columns("workflow_runs")})
    assert "ck_workflow_runs_compiled_snapshot_hash_length" not in {
        row["name"] for row in downgraded.get_check_constraints("workflow_runs")
    }
    assert "model_route_versions" not in downgraded.get_table_names()

    command.upgrade(cfg, "0064_immutable_runtime_versions")
    assert_upgraded()
    engine.dispose()


def test_0064_json_type_compiles_to_postgresql_jsonb():
    migration = _load_migration_module("0064_immutable_runtime_versions")
    assert str(migration.JSON_TYPE.compile(dialect=postgresql.dialect())) == "JSONB"


def test_0067_recipe_revision_governance_backfills_and_round_trips(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "recipe-revision-governance.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0066_version_source_fks")
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["users"].insert().values(
                id=6701,
                phone="13976700001",
                status="active",
                is_admin=False,
                balance_credits=0,
                frozen_credits=0,
            )
        )
        connection.execute(
            metadata.tables["creation_recipes"].insert().values(
                id=6702,
                user_id=6701,
                title="Legacy current title",
                category="image",
                visibility="public",
                favorite=False,
                current_version=2,
                cover_asset_url="/media/legacy-cover.png",
                moderation_status="approved",
                approved_version=2,
            )
        )
        connection.execute(
            metadata.tables["creation_recipe_versions"].insert(),
            [
                {
                    "id": 6703,
                    "recipe_id": 6702,
                    "version": 1,
                    "payload": {"prompt": "legacy version one"},
                },
                {
                    "id": 6704,
                    "recipe_id": 6702,
                    "version": 2,
                    "payload": {"prompt": "legacy version two"},
                },
            ],
        )

    def assert_upgraded() -> None:
        inspector = sa.inspect(engine)
        recipe_columns = {
            column["name"]: column
            for column in inspector.get_columns("creation_recipes")
        }
        version_columns = {
            column["name"]: column
            for column in inspector.get_columns("creation_recipe_versions")
        }
        assert recipe_columns["deleted_at"]["nullable"] is True
        assert version_columns["metadata_snapshot"]["nullable"] is False
        deleted_index = next(
            index
            for index in inspector.get_indexes("creation_recipes")
            if index["name"] == "ix_creation_recipes_deleted_at"
        )
        assert deleted_index["column_names"] == ["deleted_at"]
        assert deleted_index["unique"] == 0
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT version, payload, metadata_snapshot "
                    "FROM creation_recipe_versions "
                    "WHERE recipe_id = 6702 ORDER BY version"
                )
            ).mappings().all()
            current = connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
        assert current == "0067_recipe_revision_governance"
        assert [row["version"] for row in rows] == [1, 2]
        assert [json.loads(row["payload"])["prompt"] for row in rows] == [
            "legacy version one",
            "legacy version two",
        ]
        snapshots = [json.loads(row["metadata_snapshot"]) for row in rows]
        assert snapshots == [
            {
                "schema_version": "creation-recipe-metadata.v1",
                "origin": "legacy_backfill",
                "title": "Legacy current title",
                "visibility": "public",
                "cover_asset_url": "/media/legacy-cover.png",
            },
        ] * 2

    def assert_downgraded() -> None:
        inspector = sa.inspect(engine)
        assert "deleted_at" not in {
            column["name"] for column in inspector.get_columns("creation_recipes")
        }
        assert "metadata_snapshot" not in {
            column["name"]
            for column in inspector.get_columns("creation_recipe_versions")
        }
        assert "ix_creation_recipes_deleted_at" not in {
            index["name"] for index in inspector.get_indexes("creation_recipes")
        }
        with engine.connect() as connection:
            current = connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
            version_count = connection.scalar(
                sa.text(
                    "SELECT count(*) FROM creation_recipe_versions WHERE recipe_id = 6702"
                )
            )
        assert current == "0066_version_source_fks"
        assert version_count == 2

    command.upgrade(cfg, "0067_recipe_revision_governance")
    assert_upgraded()
    command.downgrade(cfg, "0066_version_source_fks")
    assert_downgraded()
    command.upgrade(cfg, "0067_recipe_revision_governance")
    assert_upgraded()
    engine.dispose()


def test_0067_recipe_revision_governance_blocks_soft_delete_downgrade(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "recipe-revision-governance-downgrade-guard.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0067_recipe_revision_governance")
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["users"].insert().values(
                id=6711,
                phone="13976700002",
                status="active",
                is_admin=False,
                balance_credits=0,
                frozen_credits=0,
            )
        )
        connection.execute(
            metadata.tables["creation_recipes"].insert().values(
                id=6712,
                user_id=6711,
                title="Retained soft-deleted recipe",
                category="image",
                visibility="private",
                favorite=False,
                current_version=1,
                deleted_at=datetime.now(timezone.utc),
            )
        )
        connection.execute(
            metadata.tables["creation_recipe_versions"].insert().values(
                id=6713,
                recipe_id=6712,
                version=1,
                payload={"prompt": "retained after rejected downgrade"},
                metadata_snapshot={"marker": "retained"},
            )
        )

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade recipe governance while soft-deleted recipes exist",
    ):
        command.downgrade(cfg, "0066_version_source_fks")

    inspector = sa.inspect(engine)
    assert "deleted_at" in {
        column["name"] for column in inspector.get_columns("creation_recipes")
    }
    assert "metadata_snapshot" in {
        column["name"]
        for column in inspector.get_columns("creation_recipe_versions")
    }
    assert "ix_creation_recipes_deleted_at" in {
        index["name"] for index in inspector.get_indexes("creation_recipes")
    }
    with engine.connect() as connection:
        assert connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        ) == "0067_recipe_revision_governance"
        assert connection.scalar(
            sa.text(
                "SELECT count(*) FROM creation_recipes "
                "WHERE id = 6712 AND deleted_at IS NOT NULL"
            )
        ) == 1
        retained_version = connection.execute(
            sa.text(
                "SELECT payload, metadata_snapshot FROM creation_recipe_versions "
                "WHERE id = 6713"
            )
        ).mappings().one()
    assert json.loads(retained_version["payload"]) == {
        "prompt": "retained after rejected downgrade"
    }
    assert json.loads(retained_version["metadata_snapshot"]) == {
        "marker": "retained"
    }
    engine.dispose()


def test_0068_catalog_metadata_snapshots_backfill_and_round_trip(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "catalog-metadata-snapshots.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0067_recipe_revision_governance")
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["model_configs"].insert().values(
                id=6801,
                use="image",
                model_id="legacy-catalog-image",
                display_name="Legacy Catalog Image",
                is_default=False,
                sort_order=17,
                cost_credits=9,
                unlock_cost=2,
                enabled=True,
                extra={"legacy": True},
            )
        )
        connection.execute(
            metadata.tables["model_capability_versions"].insert(),
            [
                {
                    "id": 6811,
                    "model_config_id": 6801,
                    "version": 1,
                    "schema_version": "capability.v1",
                    "capabilities": {"text_to_image": True},
                    "status": "published",
                    "is_active": True,
                    "activated_at": datetime.now(timezone.utc),
                },
                {
                    "id": 6812,
                    "model_config_id": 6801,
                    "version": 2,
                    "schema_version": "capability.v1",
                    "capabilities": {"image_to_image": True},
                    "status": "draft",
                    "is_active": False,
                    "activated_at": None,
                },
            ],
        )
        connection.execute(
            metadata.tables["tool_definitions"].insert().values(
                id=6821,
                slug="legacy-catalog-tool",
                name="Legacy Catalog Tool",
                description="Legacy tool metadata snapshot fixture",
                category="image",
                renderer="studio",
                entry_path="/?tool=legacy-catalog-tool",
                icon="wand-sparkles",
                sort_order=23,
                enabled=True,
                featured=False,
            )
        )
        connection.execute(
            metadata.tables["tool_versions"].insert(),
            [
                {
                    "id": 6822,
                    "tool_definition_id": 6821,
                    "version": 1,
                    "schema_version": "tool.v1",
                    "input_schema": {"type": "object"},
                    "workflow": {"type": "workflow.v1", "nodes": []},
                    "pricing_policy": {"fixed_credits": 3},
                    "capabilities": {"image": True},
                    "status": "published",
                    "is_active": True,
                    "activated_at": datetime.now(timezone.utc),
                },
                {
                    "id": 6823,
                    "tool_definition_id": 6821,
                    "version": 2,
                    "schema_version": "tool.v1",
                    "input_schema": {"type": "object", "required": ["prompt"]},
                    "workflow": {"type": "workflow.v1", "nodes": []},
                    "pricing_policy": {"fixed_credits": 4},
                    "capabilities": {"image": True, "draft": True},
                    "status": "draft",
                    "is_active": False,
                    "activated_at": None,
                },
            ],
        )

    expected_model_snapshot = {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "legacy_backfill",
        "model_id": "legacy-catalog-image",
        "display_name": "Legacy Catalog Image",
        "is_default": False,
        "sort_order": 17,
        "enabled": True,
    }
    expected_tool_snapshot = {
        "schema_version": "tool-catalog-metadata.v1",
        "origin": "legacy_backfill",
        "slug": "legacy-catalog-tool",
        "name": "Legacy Catalog Tool",
        "description": "Legacy tool metadata snapshot fixture",
        "category": "image",
        "renderer": "studio",
        "entry_path": "/?tool=legacy-catalog-tool",
        "icon": "wand-sparkles",
        "sort_order": 23,
        "enabled": True,
        "featured": False,
    }

    def assert_upgraded() -> None:
        inspector = sa.inspect(engine)
        for table_name in ("model_capability_versions", "tool_versions"):
            columns = {
                column["name"]: column
                for column in inspector.get_columns(table_name)
            }
            assert columns["metadata_snapshot"]["nullable"] is False
        with engine.connect() as connection:
            model_rows = connection.execute(
                sa.text(
                    "SELECT version, capabilities, metadata_snapshot "
                    "FROM model_capability_versions "
                    "WHERE model_config_id = 6801 ORDER BY version"
                )
            ).mappings().all()
            tool_rows = connection.execute(
                sa.text(
                    "SELECT version, capabilities, metadata_snapshot FROM tool_versions "
                    "WHERE tool_definition_id = 6821 ORDER BY version"
                )
            ).mappings().all()
            current = connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
        assert current == "0068_catalog_metadata_snapshots"
        assert [row["version"] for row in model_rows] == [1, 2]
        assert [json.loads(row["metadata_snapshot"]) for row in model_rows] == [
            expected_model_snapshot,
        ] * 2
        assert [json.loads(row["capabilities"]) for row in model_rows] == [
            {"text_to_image": True},
            {"image_to_image": True},
        ]
        assert [row["version"] for row in tool_rows] == [1, 2]
        assert [json.loads(row["metadata_snapshot"]) for row in tool_rows] == [
            expected_tool_snapshot,
        ] * 2
        assert [json.loads(row["capabilities"]) for row in tool_rows] == [
            {"image": True},
            {"image": True, "draft": True},
        ]

    def assert_downgraded() -> None:
        inspector = sa.inspect(engine)
        for table_name in ("model_capability_versions", "tool_versions"):
            assert "metadata_snapshot" not in {
                column["name"] for column in inspector.get_columns(table_name)
            }
        with engine.connect() as connection:
            current = connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
            model_version_count = connection.scalar(
                sa.text(
                    "SELECT count(*) FROM model_capability_versions "
                    "WHERE model_config_id = 6801"
                )
            )
            tool_version_count = connection.scalar(
                sa.text(
                    "SELECT count(*) FROM tool_versions WHERE tool_definition_id = 6821"
                )
            )
        assert current == "0067_recipe_revision_governance"
        assert model_version_count == 2
        assert tool_version_count == 2

    command.upgrade(cfg, "0068_catalog_metadata_snapshots")
    assert_upgraded()
    command.downgrade(cfg, "0067_recipe_revision_governance")
    assert_downgraded()
    command.upgrade(cfg, "0068_catalog_metadata_snapshots")
    assert_upgraded()
    engine.dispose()


@pytest.mark.parametrize(
    "revision",
    ["0067_recipe_revision_governance", "0068_catalog_metadata_snapshots"],
)
def test_catalog_metadata_snapshot_json_types_compile_to_postgresql_jsonb(revision):
    migration = _load_migration_module(revision)
    assert str(migration.JSON_TYPE.compile(dialect=postgresql.dialect())) == "JSONB"


def test_0069_reproduction_remediations_round_trip_preserves_existing_data(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "reproduction-remediations.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0068_catalog_metadata_snapshots")
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["model_configs"].insert().values(
                id=6901,
                use="image",
                model_id="pre-remediation-model",
                display_name="Pre-remediation model",
                is_default=False,
                sort_order=69,
                cost_credits=1,
                unlock_cost=0,
                enabled=True,
                extra={"migration_marker": "retained"},
            )
        )

    expected_columns = {
        "id",
        "assessment_id",
        "correction_id",
        "user_id",
        "parent_remediation_id",
        "idempotency_key",
        "request_fingerprint",
        "mode",
        "status",
        "selected_finding_ids",
        "selected_shot_ids",
        "source_asset_ref",
        "target_asset_ref",
        "applied_revision_id",
        "plan_snapshot",
        "plan_hash",
        "video_composition",
        "generation_task_ids",
        "composition_tool_run_id",
        "composition_workflow_run_id",
        "final_asset_id",
        "successor_assessment_id",
        "auto_reassess",
        "error_code",
        "error",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    }
    expected_indexes = {
        "uq_reproduction_remediations_assessment_idempotency",
        "uq_reproduction_remediations_correction",
        "uq_reproduction_remediations_applied_revision",
        "uq_reproduction_remediations_composition_tool_run",
        "uq_reproduction_remediations_composition_workflow",
        "uq_reproduction_remediations_final_asset",
        "uq_reproduction_remediations_successor_assessment",
        "ix_reproduction_remediations_assessment_created",
        "ix_reproduction_remediations_user_status_created",
        "ix_reproduction_remediations_parent",
    }
    expected_checks = {
        "ck_reproduction_remediations_mode_valid",
        "ck_reproduction_remediations_status_valid",
        "ck_reproduction_remediations_fingerprint_length",
        "ck_reproduction_remediations_plan_hash_length",
        "ck_reproduction_remediations_parent_not_self",
        "ck_reproduction_remediations_composition_pair",
    }
    expected_foreign_keys = {
        ("assessment_id", "reproduction_assessments", "id"),
        ("correction_id", "reproduction_corrections", "id"),
        ("user_id", "users", "id"),
        ("parent_remediation_id", "reproduction_remediations", "id"),
        ("applied_revision_id", "reverse_result_revisions", "id"),
        ("composition_tool_run_id", "tool_runs", "id"),
        ("composition_workflow_run_id", "workflow_runs", "id"),
        ("final_asset_id", "gen_assets", "id"),
        ("successor_assessment_id", "reproduction_assessments", "id"),
    }

    def assert_marker_retained() -> None:
        with engine.connect() as connection:
            marker = connection.scalar(
                sa.text("SELECT extra FROM model_configs WHERE id = 6901")
            )
        assert json.loads(marker) == {"migration_marker": "retained"}

    def assert_upgraded() -> None:
        inspector = sa.inspect(engine)
        assert "reproduction_remediations" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns(
            "reproduction_remediations"
        )} == expected_columns
        assert {index["name"] for index in inspector.get_indexes(
            "reproduction_remediations"
        )} == expected_indexes
        assert {check["name"] for check in inspector.get_check_constraints(
            "reproduction_remediations"
        )} == expected_checks
        foreign_keys = {
            (
                item["constrained_columns"][0],
                item["referred_table"],
                item["referred_columns"][0],
            )
            for item in inspector.get_foreign_keys("reproduction_remediations")
        }
        assert foreign_keys == expected_foreign_keys
        with engine.connect() as connection:
            current = connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
        assert current == "0069_reproduction_remediations"
        assert_marker_retained()

    command.upgrade(cfg, "0069_reproduction_remediations")
    assert_upgraded()
    command.downgrade(cfg, "0068_catalog_metadata_snapshots")
    assert "reproduction_remediations" not in sa.inspect(engine).get_table_names()
    assert_marker_retained()
    command.upgrade(cfg, "0069_reproduction_remediations")
    assert_upgraded()
    engine.dispose()


def test_0069_reproduction_remediation_json_types_compile_to_postgresql_jsonb():
    migration = _load_migration_module("0069_reproduction_remediations")
    assert str(migration.JSON_TYPE.compile(dialect=postgresql.dialect())) == "JSONB"


def test_0070_corrects_verified_video_capabilities_and_publishes_new_versions(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "video-model-capabilities.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0069_reproduction_remediations")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    seedance_metadata = {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "test",
        "model_id": "doubao-seedance-1-5-pro-251215",
        "display_name": "Seedance 1.5 Pro",
        "is_default": True,
        "sort_order": 1,
        "enabled": True,
    }
    grok_metadata = {
        **seedance_metadata,
        "model_id": "grok-imagine-video-1.5",
        "display_name": "Grok Video 1.5",
        "is_default": False,
        "sort_order": 2,
    }
    generic_metadata = {
        **grok_metadata,
        "model_id": "generic-video-model",
        "display_name": "Generic Video",
        "sort_order": 3,
    }
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            model_configs.insert(),
            [
                {
                    "id": 7001,
                    "use": "video",
                    "model_id": seedance_metadata["model_id"],
                    "display_name": seedance_metadata["display_name"],
                    "is_default": True,
                    "sort_order": 1,
                    "provider": "volcengine_ark",
                    "gateway_format": "ark",
                    "cost_credits": 50,
                    "unlock_cost": 0,
                    "enabled": True,
                    "extra": {
                        "capabilities": {
                            "multi_reference": True,
                            "max_reference_images": 10,
                            "custom_flag": "keep",
                        }
                    },
                },
                {
                    "id": 7002,
                    "use": "video",
                    "model_id": grok_metadata["model_id"],
                    "display_name": grok_metadata["display_name"],
                    "is_default": False,
                    "sort_order": 2,
                    "provider": "grok",
                    "gateway_format": "openai",
                    "cost_credits": 40,
                    "unlock_cost": 0,
                    "enabled": True,
                    "extra": {
                        "capabilities": {
                            "text_to_video": True,
                            "image_to_video": False,
                            "multi_reference": False,
                        }
                    },
                },
                {
                    "id": 7003,
                    "use": "video",
                    "model_id": generic_metadata["model_id"],
                    "display_name": generic_metadata["display_name"],
                    "is_default": False,
                    "sort_order": 3,
                    "provider": "custom_openai",
                    "gateway_format": "openai",
                    "cost_credits": 30,
                    "unlock_cost": 0,
                    "enabled": True,
                    "extra": {
                        "capabilities": {
                            "text_to_video": True,
                            "image_to_video": False,
                            "multi_reference": False,
                        }
                    },
                },
            ],
        )
        connection.execute(
            capability_versions.insert(),
            [
                {
                    "id": 7011,
                    "model_config_id": 7001,
                    "version": 1,
                    "schema_version": "capability.v1",
                    "capabilities": {
                        "multi_reference": True,
                        "max_reference_images": 10,
                        "custom_flag": "keep",
                    },
                    "metadata_snapshot": seedance_metadata,
                    "status": "published",
                    "is_active": True,
                    "activated_at": now,
                },
                {
                    "id": 7012,
                    "model_config_id": 7002,
                    "version": 1,
                    "schema_version": "capability.v1",
                    "capabilities": {
                        "text_to_video": True,
                        "image_to_video": False,
                        "multi_reference": False,
                    },
                    "metadata_snapshot": grok_metadata,
                    "status": "published",
                    "is_active": True,
                    "activated_at": now,
                },
                {
                    "id": 7013,
                    "model_config_id": 7003,
                    "version": 1,
                    "schema_version": "capability.v1",
                    "capabilities": {
                        "text_to_video": True,
                        "image_to_video": False,
                        "multi_reference": False,
                    },
                    "metadata_snapshot": generic_metadata,
                    "status": "published",
                    "is_active": True,
                    "activated_at": now,
                },
            ],
        )

    command.upgrade(cfg, "0070_video_model_capabilities")
    with engine.connect() as connection:
        model_rows = {
            row.model_id: row.extra["capabilities"]
            for row in connection.execute(
                sa.select(model_configs.c.model_id, model_configs.c.extra).where(
                    model_configs.c.id.in_([7001, 7002, 7003])
                )
            )
        }
        version_rows = connection.execute(
            sa.select(
                capability_versions.c.model_config_id,
                capability_versions.c.version,
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.source_version_id,
                capability_versions.c.capabilities,
                capability_versions.c.metadata_snapshot,
            )
            .where(capability_versions.c.model_config_id.in_([7001, 7002, 7003]))
            .order_by(
                capability_versions.c.model_config_id,
                capability_versions.c.version,
            )
        ).mappings().all()
        current = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))

    assert current == "0070_video_model_capabilities"
    assert model_rows[seedance_metadata["model_id"]] == {
        "text_to_video": True,
        "image_to_video": True,
        "first_last_frame": True,
        "multi_reference": False,
        "max_reference_images": 2,
        "custom_flag": "keep",
    }
    assert model_rows[grok_metadata["model_id"]] == {
        "text_to_video": True,
        "image_to_video": True,
        "multi_reference": False,
    }
    assert model_rows[generic_metadata["model_id"]]["image_to_video"] is False

    seedance_versions = [row for row in version_rows if row["model_config_id"] == 7001]
    assert [(row["version"], row["status"], row["is_active"]) for row in seedance_versions] == [
        (1, "disabled", False),
        (2, "published", True),
    ]
    assert seedance_versions[1]["source_version_id"] == 7011
    assert seedance_versions[1]["capabilities"] == model_rows[seedance_metadata["model_id"]]
    assert seedance_versions[1]["metadata_snapshot"] == seedance_metadata

    grok_versions = [row for row in version_rows if row["model_config_id"] == 7002]
    assert [(row["version"], row["status"], row["is_active"]) for row in grok_versions] == [
        (1, "disabled", False),
        (2, "published", True),
    ]
    assert grok_versions[1]["source_version_id"] == 7012
    generic_versions = [row for row in version_rows if row["model_config_id"] == 7003]
    assert len(generic_versions) == 1
    assert generic_versions[0]["is_active"] is True

    command.downgrade(cfg, "0069_reproduction_remediations")
    command.upgrade(cfg, "0070_video_model_capabilities")
    with engine.connect() as connection:
        counts = dict(
            connection.execute(
                sa.select(
                    capability_versions.c.model_config_id,
                    sa.func.count(capability_versions.c.id),
                )
                .where(capability_versions.c.model_config_id.in_([7001, 7002, 7003]))
                .group_by(capability_versions.c.model_config_id)
            ).all()
        )
    assert counts == {7001: 2, 7002: 2, 7003: 1}
    engine.dispose()


def test_head_has_immutable_version_source_foreign_keys(tmp_path, monkeypatch):
    db_path = tmp_path / "immutable-version-source-fks.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "head")
    inspector = sa.inspect(sa.create_engine(database_url))
    for table in (
        "model_capability_versions",
        "model_price_versions",
        "model_route_versions",
        "tool_versions",
    ):
        source_foreign_keys = [
            item
            for item in inspector.get_foreign_keys(table)
            if item["constrained_columns"] == ["source_version_id"]
        ]
        assert len(source_foreign_keys) == 1
        assert source_foreign_keys[0]["referred_table"] == table
        assert source_foreign_keys[0]["referred_columns"] == ["id"]
        assert source_foreign_keys[0]["options"]["ondelete"] == "SET NULL"


def test_0037_unified_user_assets_sqlite_round_trip(tmp_path, monkeypatch):
    db_path = tmp_path / "unified-user-assets.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0036_multi_model_catalog")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    before = sa.inspect(engine)
    assert "retained_at" not in {c["name"] for c in before.get_columns("gen_assets")}
    assert "favorite" not in {c["name"] for c in before.get_columns("uploaded_assets")}

    command.upgrade(cfg, "0037_unified_user_assets")
    upgraded = sa.inspect(engine)
    assert {"bytes", "retained_at"} <= {
        c["name"] for c in upgraded.get_columns("gen_assets")
    }
    assert {"duration", "favorite", "retained_at"} <= {
        c["name"] for c in upgraded.get_columns("uploaded_assets")
    }
    assert {
        "ix_gen_assets_user_created",
        "ix_gen_assets_user_favorite_created",
        "ix_gen_assets_user_retained",
    } <= {idx["name"] for idx in upgraded.get_indexes("gen_assets")}
    assert {
        "ix_uploaded_assets_user_created",
        "ix_uploaded_assets_user_favorite_created",
        "ix_uploaded_assets_user_retained",
    } <= {idx["name"] for idx in upgraded.get_indexes("uploaded_assets")}

    command.downgrade(cfg, "0036_multi_model_catalog")
    downgraded = sa.inspect(engine)
    assert "retained_at" not in {c["name"] for c in downgraded.get_columns("gen_assets")}
    assert "favorite" not in {c["name"] for c in downgraded.get_columns("uploaded_assets")}

    command.upgrade(cfg, "0037_unified_user_assets")
    reupgraded = sa.inspect(engine)
    assert "retained_at" in {c["name"] for c in reupgraded.get_columns("gen_assets")}
    assert "favorite" in {c["name"] for c in reupgraded.get_columns("uploaded_assets")}


def test_0038_declares_multi_reference_only_for_ark_seedance_2(tmp_path, monkeypatch):
    db_path = tmp_path / "seedance-multi-reference.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0037_unified_user_assets")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(
            model_configs.insert(),
            [
                {
                    "use": "video",
                    "model_id": "doubao-seedance-2-0-mini-260615",
                    "display_name": "Seedance 2.0 Mini",
                    "is_default": True,
                    "sort_order": 0,
                    "provider": "volcengine_ark",
                    "gateway_format": "ark",
                    "cost_credits": 16,
                    "unlock_cost": 0,
                    "enabled": True,
                    "extra": {"preview_cost": 15},
                },
                {
                    "use": "video",
                    "model_id": "generic-video-model",
                    "display_name": "Generic Video",
                    "is_default": False,
                    "sort_order": 1,
                    "provider": "custom",
                    "gateway_format": "openai",
                    "cost_credits": 16,
                    "unlock_cost": 0,
                    "enabled": True,
                    "extra": {},
                },
            ],
        )

    command.upgrade(cfg, "0038_seedance_multi_reference")
    with engine.connect() as conn:
        rows = {
            row.model_id: row.extra
            for row in conn.execute(
                sa.select(model_configs.c.model_id, model_configs.c.extra)
            )
        }
    assert rows["doubao-seedance-2-0-mini-260615"]["capabilities"] == {
        "multi_reference": True,
        "max_reference_images": 10,
    }
    assert "capabilities" not in rows["generic-video-model"]


def test_0039_applies_target_margin_packages_and_model_prices(tmp_path, monkeypatch):
    db_path = tmp_path / "margin-credit-pricing.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0038_seedance_multi_reference")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    payment_packages = sa.Table("payment_packages", metadata, autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(
            model_configs.update()
            .where(model_configs.c.use == "prompt")
            .values(cost_credits=1, unlock_cost=4)
        )
        conn.execute(
            model_configs.insert(),
            [
                {
                    "use": "vision",
                    "model_id": "vision-test",
                    "display_name": "Vision Test",
                    "is_default": True,
                    "sort_order": 0,
                    "cost_credits": 10,
                    "unlock_cost": 3,
                    "enabled": True,
                    "extra": {"keep": "vision"},
                },
                {
                    "use": "image",
                    "model_id": "image-test",
                    "display_name": "Image Test",
                    "is_default": True,
                    "sort_order": 0,
                    "cost_credits": 15,
                    "unlock_cost": 2,
                    "enabled": True,
                    "extra": {"keep": "image"},
                },
                {
                    "use": "video",
                    "model_id": "video-test",
                    "display_name": "Video Test",
                    "is_default": True,
                    "sort_order": 0,
                    "cost_credits": 16,
                    "unlock_cost": 1,
                    "enabled": True,
                    "extra": {"preview_cost": 15, "submit_path": "/custom/submit"},
                },
            ],
        )
        conn.execute(
            payment_packages.update()
            .where(payment_packages.c.id == "starter")
            .values(amount_cents=990, credits=100, enabled=False)
        )

    command.upgrade(cfg, "0039_margin_credit_pricing")
    with engine.connect() as conn:
        packages = [
            tuple(row)
            for row in conn.execute(
                sa.select(
                    payment_packages.c.id,
                    payment_packages.c.amount_cents,
                    payment_packages.c.credits,
                    payment_packages.c.enabled,
                )
                .where(
                    payment_packages.c.id.in_(
                        ["starter", "creator", "pro", "team", "enterprise"]
                    )
                )
                .order_by(payment_packages.c.sort_order)
            )
        ]
        model_rows = {
            row.use: (row.cost_credits, row.unlock_cost, row.extra)
            for row in conn.execute(
                sa.select(
                    model_configs.c.use,
                    model_configs.c.cost_credits,
                    model_configs.c.unlock_cost,
                    model_configs.c.extra,
                )
            )
        }

    assert packages == [
        ("starter", 2990, 300, True),
        ("creator", 9900, 1050, True),
        ("pro", 29900, 3300, True),
        ("team", 69900, 8000, True),
        ("enterprise", 99900, 12000, True),
    ]
    assert {use: values[:2] for use, values in model_rows.items()} == {
        "prompt": (3, 0),
        "vision": (5, 0),
        "image": (8, 0),
        "video": (100, 0),
    }
    assert model_rows["video"][2] == {
        "preview_cost": 50,
        "submit_path": "/custom/submit",
    }
    assert model_rows["vision"][2] == {"keep": "vision"}
    assert model_rows["image"][2] == {"keep": "image"}

    full_capacity_costs = [
        (5, 0.2),
        (3, 0.1),
        (8, 0.3),
        (100, 3.125 + 105 / 160),
        (108, 3.125 + 105 / 160 + 0.2 + 0.1),
        (116, 3.125 + 105 / 160 + 0.2 + 0.1 + 0.3),
    ]
    revenue_per_credit = [29.9 / 300, 99 / 1050, 299 / 3300, 699 / 8000, 999 / 12000]
    for unit_revenue in revenue_per_credit:
        for credits, cost in full_capacity_costs:
            revenue = credits * unit_revenue
            gross_margin = (revenue - cost) / revenue
            assert 0.5 <= gross_margin <= 0.7


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
    assert not model_config_indexes["ix_model_configs_use"]["unique"]
    assert model_config_indexes["uq_model_configs_default_per_use"]["unique"]
    assert model_config_indexes["uq_model_configs_use_model_id"]["unique"]
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


def test_0035_backfills_legacy_reverse_cost_and_lifecycle_timestamps(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-0035-backfill.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0034_anthropic_gateway_format")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "insert into users "
            "(id, phone, password_hash, balance_credits, frozen_credits, is_admin, status) "
            "values (1, '13900000998', 'hash', 0, 0, 0, 'active')"
        ))
        conn.execute(sa.text(
            "insert into reverse_operations "
            "(id, user_id, client_request_id, request_fingerprint, target, asset_url, "
            "status, charged_credits, created_at, updated_at) values "
            "(1, 1, 'legacy-success', :success_fp, 'image', 'https://cdn/a.jpg', "
            "'succeeded', 7, '2026-07-01 10:00:00', '2026-07-01 10:01:00'), "
            "(2, 1, 'legacy-failed', :failed_fp, 'image', 'https://cdn/b.jpg', "
            "'failed', 0, '2026-07-01 11:00:00', '2026-07-01 11:01:00')"
        ), {"success_fp": "e" * 64, "failed_fp": "f" * 64})

    command.upgrade(cfg, "0035_async_reverse_operations")
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "select id, cost_settled, progress, finished_at "
            "from reverse_operations order by id"
        )).all()
    assert rows[0][1:3] == (7, 100)
    assert rows[1][1:3] == (0, 100)
    assert rows[0][3] is not None
    assert rows[1][3] is not None

    command.downgrade(cfg, "0034_anthropic_gateway_format")
    columns = {column["name"] for column in sa.inspect(engine).get_columns("reverse_operations")}
    assert "cost_settled" not in columns
    command.upgrade(cfg, "0035_async_reverse_operations")
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "select cost_settled from reverse_operations where id = 1"
        )).scalar_one() == 7


def test_0035_downgrade_blocks_async_only_statuses(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-0035-active.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0035_async_reverse_operations")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "insert into users "
            "(id, phone, password_hash, balance_credits, frozen_credits, is_admin, status) "
            "values (1, '13900000999', 'hash', 0, 0, 0, 'active')"
        ))
        conn.execute(sa.text(
            "insert into reverse_operations "
            "(id, user_id, client_request_id, request_fingerprint, target, asset_url, status) "
            "values (1, 1, 'async-queued', :fingerprint, 'image', 'https://cdn/a.jpg', 'queued')"
        ), {"fingerprint": "9" * 64})

    with pytest.raises(RuntimeError, match="asynchronous reverse operations exist"):
        command.downgrade(cfg, "0034_anthropic_gateway_format")


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


def test_0051_aligns_reverse_fingerprints_with_postgres_jsonb(monkeypatch):
    migration = _load_migration_module("0051_reverse_fingerprints_jsonb")
    calls = []
    bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})()})()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda table, column, **kwargs: calls.append((table, column, kwargs)),
    )

    migration.upgrade()
    assert len(calls) == 1
    table, column, kwargs = calls.pop()
    assert (table, column) == ("reverse_result_revisions", "source_fingerprints")
    assert isinstance(kwargs["existing_type"], postgresql.JSON)
    assert isinstance(kwargs["type_"], postgresql.JSONB)
    assert kwargs["postgresql_using"] == '"source_fingerprints"::jsonb'

    migration.downgrade()
    assert len(calls) == 1
    table, column, kwargs = calls.pop()
    assert (table, column) == ("reverse_result_revisions", "source_fingerprints")
    assert isinstance(kwargs["existing_type"], postgresql.JSONB)
    assert isinstance(kwargs["type_"], postgresql.JSON)
    assert kwargs["postgresql_using"] == '"source_fingerprints"::json'


def test_0051_reverse_fingerprints_is_a_noop_outside_postgresql(monkeypatch):
    migration = _load_migration_module("0051_reverse_fingerprints_jsonb")
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


def test_0047_reverse_revision_lineage_backfills_and_round_trips(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    script = ScriptDirectory(str(backend / "alembic"))
    revision = script.get_revision("0047_reverse_revision_lineage")
    assert revision is not None
    assert revision.down_revision == "0046_generation_lineage"

    db_path = tmp_path / "reverse-revision-lineage.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0046_generation_lineage")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=engine)
    operations = sa.Table("reverse_operations", metadata, autoload_with=engine)
    revisions = sa.Table("reverse_result_revisions", metadata, autoload_with=engine)
    sources = (
        "provider_raw",
        "normalized",
        "user_edit",
        "applied",
        "model_compiled",
        "generation",
    )
    with engine.begin() as conn:
        conn.execute(users.insert().values(
            id=4701,
            phone="13974700001",
            password_hash="migration-test",
            status="active",
            is_admin=False,
            balance_credits=0,
            frozen_credits=0,
        ))
        conn.execute(operations.insert().values(
            id=4702,
            user_id=4701,
            request_fingerprint="4" * 64,
            target="image",
            asset_url="http://example.com/legacy-lineage.png",
            status="succeeded",
            result={"final_text": "legacy"},
            charged_credits=0,
            reference_count=1,
        ))
        conn.execute(revisions.insert(), [
            {
                "id": 4710 + version,
                "operation_id": 4702,
                "user_id": 4701,
                "version": version,
                "source": source,
                "payload": {"source": source, "version": version},
            }
            for version, source in enumerate(sources, start=1)
        ])

    expected_columns = {
        "parent_revision_id",
        "source_content_hash",
        "source_fingerprints",
        "payload_hash",
        "lineage_status",
        "evidence_review_action",
    }
    expected_checks = {
        "ck_reverse_result_revisions_lineage_status_valid",
        "ck_reverse_result_revisions_evidence_action_valid",
        "ck_reverse_result_revisions_parent_not_self",
    }
    expected_indexes = {
        "ix_reverse_result_revisions_user_id",
        "ix_reverse_result_revisions_parent_revision_id",
        "ix_reverse_result_revisions_lineage_status",
    }

    def assert_upgraded_schema_and_rows() -> None:
        inspector = sa.inspect(engine)
        assert expected_columns <= {
            column["name"]
            for column in inspector.get_columns("reverse_result_revisions")
        }
        assert expected_checks <= {
            constraint["name"]
            for constraint in inspector.get_check_constraints("reverse_result_revisions")
        }
        assert expected_indexes <= {
            index["name"]
            for index in inspector.get_indexes("reverse_result_revisions")
        }
        parent_fk = next(
            foreign_key
            for foreign_key in inspector.get_foreign_keys("reverse_result_revisions")
            if foreign_key["name"] == "fk_reverse_result_revisions_parent_revision_id"
        )
        assert parent_fk["constrained_columns"] == ["parent_revision_id"]
        assert parent_fk["referred_table"] == "reverse_result_revisions"
        assert parent_fk["referred_columns"] == ["id"]

        current = sa.Table(
            "reverse_result_revisions",
            sa.MetaData(),
            autoload_with=engine,
        )
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    current.c.id,
                    current.c.parent_revision_id,
                    current.c.lineage_status,
                )
                .where(current.c.operation_id == 4702)
                .order_by(current.c.version)
            ).all()
        ids = [4710 + version for version in range(1, 7)]
        assert [row.id for row in rows] == ids
        assert [row.parent_revision_id for row in rows] == [None, *ids[:-1]]
        assert {row.lineage_status for row in rows} == {"legacy_unverified"}

    command.upgrade(cfg, "0047_reverse_revision_lineage")
    assert_upgraded_schema_and_rows()

    command.downgrade(cfg, "0046_generation_lineage")
    downgraded_columns = {
        column["name"]
        for column in sa.inspect(engine).get_columns("reverse_result_revisions")
    }
    assert expected_columns.isdisjoint(downgraded_columns)
    downgraded_indexes = {
        index["name"]
        for index in sa.inspect(engine).get_indexes("reverse_result_revisions")
    }
    assert expected_indexes.isdisjoint(downgraded_indexes)

    command.upgrade(cfg, "0047_reverse_revision_lineage")
    assert_upgraded_schema_and_rows()
    engine.dispose()


def test_0048_generation_dispatch_outbox_round_trips(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "generation-dispatch-outbox.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0047_reverse_revision_lineage")

    engine = sa.create_engine(database_url)

    def assert_upgraded() -> None:
        inspector = sa.inspect(engine)
        assert "generation_dispatches" in inspector.get_table_names()
        assert {
            "id",
            "task_id",
            "attempt",
            "task_name",
            "celery_task_id",
            "status",
            "publish_attempts",
            "next_attempt_at",
            "last_error_code",
            "last_error_at",
            "published_at",
            "completed_at",
            "created_at",
            "updated_at",
        } == {column["name"] for column in inspector.get_columns("generation_dispatches")}
        assert {
            "ix_generation_dispatches_task_id",
            "uq_generation_dispatches_task_attempt",
            "uq_generation_dispatches_celery_task_id",
            "ix_generation_dispatches_reconcile",
        } <= {index["name"] for index in inspector.get_indexes("generation_dispatches")}
        assert {
            "ck_generation_dispatches_attempt_positive",
            "ck_generation_dispatches_publish_attempts_nonnegative",
            "ck_generation_dispatches_status_valid",
        } <= {
            constraint["name"]
            for constraint in inspector.get_check_constraints("generation_dispatches")
        }

    command.upgrade(cfg, "0048_generation_dispatch_outbox")
    assert_upgraded()
    command.downgrade(cfg, "0047_reverse_revision_lineage")
    assert "generation_dispatches" not in sa.inspect(engine).get_table_names()
    command.upgrade(cfg, "0048_generation_dispatch_outbox")
    assert_upgraded()
    engine.dispose()


def test_0049_prompt_optimization_proposals_round_trips_with_bounded_revision_id(
    tmp_path, monkeypatch
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "prompt-optimization-proposals.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0048_generation_dispatch_outbox")
    engine = sa.create_engine(database_url)
    assert "prompt_optimization_proposals" not in sa.inspect(engine).get_table_names()

    command.upgrade(cfg, "0049_prompt_opt_proposals")
    assert "prompt_optimization_proposals" in sa.inspect(engine).get_table_names()
    with engine.connect() as connection:
        current = connection.scalar(sa.text("select version_num from alembic_version"))
    assert current == "0049_prompt_opt_proposals"
    assert len(current) <= 32

    command.downgrade(cfg, "0048_generation_dispatch_outbox")
    assert "prompt_optimization_proposals" not in sa.inspect(engine).get_table_names()
    command.upgrade(cfg, "0049_prompt_opt_proposals")
    assert "prompt_optimization_proposals" in sa.inspect(engine).get_table_names()
    engine.dispose()


def test_0052_generation_retry_chain_round_trips(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "generation-retry-chain.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0051_reverse_fingerprints_jsonb")
    engine = sa.create_engine(database_url)

    def assert_downgraded() -> None:
        inspector = sa.inspect(engine)
        assert "retry_of_task_id" not in {
            column["name"] for column in inspector.get_columns("gen_tasks")
        }
        assert "ix_gen_tasks_retry_of_task_id" not in {
            index["name"] for index in inspector.get_indexes("gen_tasks")
        }
        assert "ck_gen_tasks_retry_not_self" not in {
            constraint["name"] for constraint in inspector.get_check_constraints("gen_tasks")
        }

    def assert_upgraded() -> None:
        inspector = sa.inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("gen_tasks")}
        assert columns["retry_of_task_id"]["nullable"] is True
        assert "ix_gen_tasks_retry_of_task_id" in {
            index["name"] for index in inspector.get_indexes("gen_tasks")
        }
        assert "ck_gen_tasks_retry_not_self" in {
            constraint["name"] for constraint in inspector.get_check_constraints("gen_tasks")
        }
        retry_fk = next(
            foreign_key
            for foreign_key in inspector.get_foreign_keys("gen_tasks")
            if foreign_key["name"] == "fk_gen_tasks_retry_of_task_id_gen_tasks"
        )
        assert retry_fk["constrained_columns"] == ["retry_of_task_id"]
        assert retry_fk["referred_table"] == "gen_tasks"
        assert retry_fk["referred_columns"] == ["id"]
        with engine.connect() as connection:
            current = connection.scalar(sa.text("select version_num from alembic_version"))
        assert current == "0052_generation_retry_chain"

    assert_downgraded()
    command.upgrade(cfg, "0052_generation_retry_chain")
    assert_upgraded()
    command.downgrade(cfg, "0051_reverse_fingerprints_jsonb")
    assert_downgraded()
    command.upgrade(cfg, "0052_generation_retry_chain")
    assert_upgraded()
    engine.dispose()


def test_0053_tool_workflow_engine_round_trips(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "tool-workflow-engine.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0052_generation_retry_chain")
    engine = sa.create_engine(database_url)

    expected_tables = {
        "tool_runs",
        "workflow_runs",
        "tool_node_runs",
        "tool_node_attempts",
    }

    def assert_downgraded() -> None:
        assert expected_tables.isdisjoint(sa.inspect(engine).get_table_names())

    def assert_upgraded() -> None:
        inspector = sa.inspect(engine)
        assert expected_tables <= set(inspector.get_table_names())
        assert {
            "workflow_run_id",
            "node_key",
            "node_type",
            "depends_on",
            "status",
            "attempt_count",
            "revision",
            "dispatch_token",
            "compensation_status",
        } <= {column["name"] for column in inspector.get_columns("tool_node_runs")}
        assert {
            "uq_tool_node_runs_workflow_node_key",
            "ix_tool_node_runs_workflow_topology",
            "ix_tool_node_runs_status_available",
        } <= {index["name"] for index in inspector.get_indexes("tool_node_runs")}
        assert {
            "ck_tool_node_runs_type_valid",
            "ck_tool_node_runs_status_valid",
            "ck_tool_node_runs_compensation_status_valid",
        } <= {
            constraint["name"]
            for constraint in inspector.get_check_constraints("tool_node_runs")
        }
        workflow_fk = next(
            foreign_key
            for foreign_key in inspector.get_foreign_keys("tool_node_runs")
            if foreign_key["constrained_columns"] == ["workflow_run_id"]
        )
        assert workflow_fk["referred_table"] == "workflow_runs"
        with engine.connect() as connection:
            current = connection.scalar(sa.text("select version_num from alembic_version"))
        assert current == "0053_tool_workflow_engine"

    assert_downgraded()
    command.upgrade(cfg, "0053_tool_workflow_engine")
    assert_upgraded()
    command.downgrade(cfg, "0052_generation_retry_chain")
    assert_downgraded()
    command.upgrade(cfg, "0053_tool_workflow_engine")
    assert_upgraded()
    engine.dispose()


def test_0054_recipe_content_governance_round_trips(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "recipe-content-governance.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0053_tool_workflow_engine")
    engine = sa.create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users "
                "(id, phone, status, is_admin, balance_credits, frozen_credits) "
                "VALUES (1, '13800000000', 'active', 0, 0, 0)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO creation_recipes "
                "(id, user_id, title, category, visibility, favorite, current_version) "
                "VALUES (1, 1, 'legacy public recipe', 'image', 'public', 0, 2)"
            )
        )

    expected_tables = {
        "creation_recipe_shares",
        "creation_recipe_usage_events",
    }
    expected_columns = {
        "moderation_status",
        "approved_version",
        "submitted_at",
        "reviewed_at",
        "reviewed_by",
        "review_note",
    }

    def assert_downgraded() -> None:
        inspector = sa.inspect(engine)
        assert expected_tables.isdisjoint(inspector.get_table_names())
        recipe_columns = {
            column["name"] for column in inspector.get_columns("creation_recipes")
        }
        assert expected_columns.isdisjoint(recipe_columns)

    def assert_upgraded() -> None:
        inspector = sa.inspect(engine)
        assert expected_tables <= set(inspector.get_table_names())
        recipe_columns = {
            column["name"] for column in inspector.get_columns("creation_recipes")
        }
        assert expected_columns <= recipe_columns
        assert {
            "ix_creation_recipes_reviewed_by",
            "ix_creation_recipes_moderation_updated",
        } <= {index["name"] for index in inspector.get_indexes("creation_recipes")}
        assert {
            "ck_creation_recipes_moderation_status_valid",
            "ck_creation_recipes_approved_version_positive",
        } <= {
            constraint["name"]
            for constraint in inspector.get_check_constraints("creation_recipes")
        }
        with engine.connect() as connection:
            current = connection.scalar(sa.text("select version_num from alembic_version"))
            legacy_recipe = connection.execute(
                sa.text(
                    "SELECT moderation_status, approved_version, submitted_at, reviewed_at "
                    "FROM creation_recipes WHERE id = 1"
                )
            ).one()
        assert current == "0054_recipe_content_governance"
        assert legacy_recipe.moderation_status == "approved"
        assert legacy_recipe.approved_version == 2
        assert legacy_recipe.submitted_at is not None
        assert legacy_recipe.reviewed_at is not None

    assert_downgraded()
    command.upgrade(cfg, "0054_recipe_content_governance")
    assert_upgraded()
    command.downgrade(cfg, "0053_tool_workflow_engine")
    assert_downgraded()
    command.upgrade(cfg, "0054_recipe_content_governance")
    assert_upgraded()
    engine.dispose()


def test_0057_storyboard_compose_tool_round_trips(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "storyboard-compose-tool.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0056_workflow_dispatch_outbox")
    engine = sa.create_engine(database_url)

    metadata = sa.MetaData()
    definitions = sa.Table("tool_definitions", metadata, autoload_with=engine)
    versions = sa.Table("tool_versions", metadata, autoload_with=engine)

    def load_tool():
        with engine.connect() as connection:
            return connection.execute(
                sa.select(
                    definitions.c.slug,
                    definitions.c.enabled,
                    versions.c.schema_version,
                    versions.c.input_schema,
                    versions.c.workflow,
                    versions.c.capabilities,
                )
                .join(versions, versions.c.tool_definition_id == definitions.c.id)
                .where(definitions.c.slug == "storyboard-compose")
            ).one_or_none()

    assert load_tool() is None
    command.upgrade(cfg, "0057_storyboard_compose_tool")
    row = load_tool()
    assert row is not None
    assert row.enabled is True
    assert row.schema_version == "tool.v1"
    assert row.input_schema["required"] == ["composition"]
    assert row.capabilities["audio_mix"] is True
    spec = WorkflowSpec.model_validate(row.workflow)
    assert spec.studio_preset is not None
    assert spec.studio_preset.creation_mode == "video_edit"
    assert spec.studio_preset.analysis_focus == "storyboard"
    assert spec.topological_keys() == ["compose", "export"]
    assert spec.nodes[0].config["timeout_seconds"] == 2400
    assert spec.nodes[0].compensation.type == "cleanup_video_composition"
    with engine.connect() as connection:
        assert connection.scalar(sa.text("select version_num from alembic_version")) == (
            "0057_storyboard_compose_tool"
        )

    command.downgrade(cfg, "0056_workflow_dispatch_outbox")
    assert load_tool() is None
    command.upgrade(cfg, "0057_storyboard_compose_tool")
    assert load_tool() is not None
    engine.dispose()


def test_0058_workflow_project_tasks_round_trips(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "workflow-project-tasks.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0057_storyboard_compose_tool")
    engine = sa.create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO users "
            "(id, phone, status, is_admin, balance_credits, frozen_credits) "
            "VALUES (5801, '13975800001', 'active', 0, 0, 0)"
        ))
        connection.execute(sa.text(
            "INSERT INTO media_projects "
            "(id, user_id, title, project_type, status) "
            "VALUES (5802, 5801, '工作流迁移项目', 'video', 'active')"
        ))

    def kind_check_sql() -> str:
        constraint = next(
            item
            for item in sa.inspect(engine).get_check_constraints("media_project_tasks")
            if item["name"] == "ck_media_project_tasks_kind_valid"
        )
        return str(constraint["sqltext"])

    assert "workflow" not in kind_check_sql()
    command.upgrade(cfg, "0058_workflow_project_tasks")
    assert "workflow" in kind_check_sql()
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO media_project_tasks "
            "(project_id, task_kind, task_id) VALUES (5802, 'workflow', 5803)"
        ))
        current = connection.scalar(sa.text("select version_num from alembic_version"))
    assert current == "0058_workflow_project_tasks"

    command.downgrade(cfg, "0057_storyboard_compose_tool")
    assert "workflow" not in kind_check_sql()
    with engine.connect() as connection:
        assert connection.scalar(sa.text(
            "SELECT count(*) FROM media_project_tasks WHERE task_kind = 'workflow'"
        )) == 0
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO media_project_tasks "
            "(project_id, task_kind, task_id) VALUES (5802, 'workflow', 5804)"
        ))

    command.upgrade(cfg, "0058_workflow_project_tasks")
    assert "workflow" in kind_check_sql()
    engine.dispose()


def test_0071_removes_unreferenced_migration_model_on_downgrade(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "gemini-vision-downgrade.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0071_gemini_31_pro_high_vision")
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(sa.text(
            "SELECT count(*) FROM model_configs "
            "WHERE use = 'vision' AND model_id = 'gemini-3.1-pro-high'"
        )) == 1

    command.downgrade(cfg, "0070_video_model_capabilities")
    with engine.connect() as connection:
        assert connection.scalar(sa.text(
            "SELECT count(*) FROM model_configs "
            "WHERE use = 'vision' AND model_id = 'gemini-3.1-pro-high'"
        )) == 0
    engine.dispose()


def test_0071_blocks_downgrade_when_migration_model_is_referenced(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "referenced-gemini-vision-downgrade.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0071_gemini_31_pro_high_vision")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        model_config_id = connection.scalar(sa.text(
            "SELECT id FROM model_configs "
            "WHERE use = 'vision' AND model_id = 'gemini-3.1-pro-high'"
        ))
        connection.execute(sa.text(
            "INSERT INTO users "
            "(id, phone, password_hash, balance_credits, frozen_credits, is_admin, status) "
            "VALUES (7101, '13900007101', 'hash', 0, 0, 0, 'active')"
        ))
        connection.execute(
            sa.text(
                "INSERT INTO reverse_operations "
                "(id, user_id, model_config_id, client_request_id, request_fingerprint, "
                "target, asset_url, status) VALUES "
                "(7102, 7101, :model_config_id, 'gemini-history', :fingerprint, "
                "'image', 'https://cdn.example.com/gemini-history.jpg', 'succeeded')"
            ),
            {"model_config_id": model_config_id, "fingerprint": "7" * 64},
        )

    with pytest.raises(
        RuntimeError,
        match=r"referenced by reverse_operations\.model_config_id",
    ):
        command.downgrade(cfg, "0070_video_model_capabilities")
    engine.dispose()


def test_0072_skips_unconfigured_image_edit_models(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "unconfigured-image-edit-models.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))

    command.upgrade(cfg, "0072_image_edit_models")
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(sa.text(
            "SELECT count(*) FROM model_configs WHERE use = 'image' "
            "AND model_id IN ('gemini-3.1-flash-image', 'grok-imagine-image')"
        )) == 0
    engine.dispose()


def test_0072_enables_verified_grok_and_gemini_image_edit_models(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "image-edit-models.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0071_gemini_31_pro_high_vision")
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    with engine.begin() as connection:
        connection.execute(
            model_configs.insert(),
            [
                {
                    "use": "image",
                    "model_id": "gemini-3.1-flash-image",
                    "display_name": "gemini-3.1-flash-image",
                    "is_default": False,
                    "sort_order": 30,
                    "provider": "antigravity",
                    "base_url": "https://models.example.com/antigravity",
                    "api_key_encrypted": "encrypted-gemini-key",
                    "gateway_format": "anthropic",
                    "cost_credits": 8,
                    "unlock_cost": 0,
                    "enabled": True,
                    "extra": {
                        "image_transport": "anthropic_messages",
                        "capabilities": {
                            "text_to_image": True,
                            "image_to_image": False,
                        },
                    },
                },
                {
                    "use": "image",
                    "model_id": "grok-imagine-image",
                    "display_name": "grok-imagine-image",
                    "is_default": False,
                    "sort_order": 40,
                    "provider": "grok",
                    "base_url": "https://models.example.com/v1",
                    "api_key_encrypted": "encrypted-grok-key",
                    "gateway_format": "openai",
                    "cost_credits": 8,
                    "unlock_cost": 0,
                    "enabled": True,
                    "extra": {
                        "image_transport": "grok_images",
                        "capabilities": {
                            "text_to_image": True,
                            "image_to_image": False,
                        },
                    },
                },
            ],
        )

    command.upgrade(cfg, "0072_image_edit_models")
    with engine.connect() as connection:
        rows = {
            row.model_id: row
            for row in connection.execute(
                sa.select(
                    model_configs.c.id,
                    model_configs.c.model_id,
                    model_configs.c.display_name,
                    model_configs.c.extra,
                ).where(
                    model_configs.c.model_id.in_(
                        ["gemini-3.1-flash-image", "grok-imagine-image"]
                    )
                )
            )
        }
        active_versions = {
            int(row.model_config_id): row.capabilities
            for row in connection.execute(
                sa.select(
                    capability_versions.c.model_config_id,
                    capability_versions.c.capabilities,
                ).where(capability_versions.c.is_active.is_(True))
            )
        }

    gemini = rows["gemini-3.1-flash-image"]
    grok = rows["grok-imagine-image"]
    assert gemini.display_name == "Gemini 3.1 Flash Image"
    assert gemini.extra["edit_path"] == "/messages"
    assert gemini.extra["capabilities"]["image_to_image"] is True
    assert gemini.extra["capabilities"]["max_reference_images"] == 2
    assert grok.display_name == "Grok Imagine Image"
    assert grok.extra["edit_path"] == "/images/edits"
    assert grok.extra["edit_payload_format"] == "json"
    assert grok.extra["capabilities"]["image_to_image"] is True
    assert grok.extra["capabilities"]["max_reference_images"] == 3
    assert active_versions[int(gemini.id)] == gemini.extra["capabilities"]
    assert active_versions[int(grok.id)] == grok.extra["capabilities"]
    engine.dispose()


def test_0073_enables_seedance_15_two_image_references_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "seedance-15-two-images.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0072_image_edit_models")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    model_id = "doubao-seedance-1-5-pro-251215"
    metadata_snapshot = {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "test",
        "model_id": model_id,
        "display_name": "Seedance 1.5 Pro",
        "is_default": False,
        "sort_order": 20,
        "enabled": True,
    }
    old_capabilities = {
        "text_to_video": True,
        "image_to_video": True,
        "first_last_frame": True,
        "multi_reference": False,
        "max_reference_images": 2,
        "custom_flag": "keep",
    }
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        result = connection.execute(
            model_configs.insert().values(
                use="video",
                model_id=model_id,
                display_name="Seedance 1.5 Pro",
                is_default=False,
                sort_order=20,
                provider="volcengine_ark",
                gateway_format="ark",
                cost_credits=100,
                unlock_cost=0,
                enabled=True,
                extra={
                    "preview_cost": 50,
                    "capabilities": old_capabilities,
                },
            )
        )
        model_config_id = int(result.inserted_primary_key[0])
        connection.execute(
            capability_versions.insert().values(
                model_config_id=model_config_id,
                version=1,
                schema_version="capability.v1",
                capabilities=old_capabilities,
                metadata_snapshot=metadata_snapshot,
                status="published",
                is_active=True,
                activated_at=now,
            )
        )

    command.upgrade(cfg, "0073_seedance_15_multi_reference")
    expected_capabilities = {
        **old_capabilities,
        "multi_reference": True,
    }
    with engine.connect() as connection:
        model_extra = connection.scalar(
            sa.select(model_configs.c.extra).where(model_configs.c.id == model_config_id)
        )
        versions = connection.execute(
            sa.select(
                capability_versions.c.version,
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.source_version_id,
                capability_versions.c.capabilities,
                capability_versions.c.metadata_snapshot,
            )
            .where(capability_versions.c.model_config_id == model_config_id)
            .order_by(capability_versions.c.version)
        ).mappings().all()

    assert model_extra["preview_cost"] == 50
    assert model_extra["capabilities"] == expected_capabilities
    assert [
        (row["version"], row["status"], row["is_active"])
        for row in versions
    ] == [
        (1, "disabled", False),
        (2, "published", True),
    ]
    assert versions[1]["source_version_id"] is not None
    assert versions[1]["capabilities"] == expected_capabilities
    assert versions[1]["metadata_snapshot"] == metadata_snapshot

    command.downgrade(cfg, "0072_image_edit_models")
    command.upgrade(cfg, "0073_seedance_15_multi_reference")
    with engine.connect() as connection:
        version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id == model_config_id
            )
        )
    assert version_count == 2
    engine.dispose()


def test_0073_does_not_enable_unverified_seedance_provider(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "seedance-15-unverified-provider.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0072_image_edit_models")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    old_capabilities = {
        "text_to_video": True,
        "image_to_video": False,
        "multi_reference": False,
    }
    with engine.begin() as connection:
        result = connection.execute(
            model_configs.insert().values(
                use="video",
                model_id="doubao-seedance-1-5-pro-251215",
                display_name="Unverified Seedance Proxy",
                is_default=False,
                sort_order=30,
                provider="custom_openai",
                gateway_format="openai",
                cost_credits=100,
                unlock_cost=0,
                enabled=True,
                extra={"capabilities": old_capabilities},
            )
        )
        model_config_id = int(result.inserted_primary_key[0])

    command.upgrade(cfg, "0073_seedance_15_multi_reference")
    with engine.connect() as connection:
        model_extra = connection.scalar(
            sa.select(model_configs.c.extra).where(model_configs.c.id == model_config_id)
        )
        version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id == model_config_id
            )
        )

    assert model_extra["capabilities"] == old_capabilities
    assert version_count == 0
    engine.dispose()


def test_0074_enables_all_ark_seedance_20_plus_models_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "seedance-20-plus-ten-images.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0073_seedance_15_multi_reference")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    old_capabilities = {
        "text_to_video": True,
        "image_to_video": False,
        "multi_reference": False,
        "max_reference_images": 3,
        "custom_flag": "keep",
    }
    expected_capabilities = {
        **old_capabilities,
        "image_to_video": True,
        "multi_reference": True,
        "max_reference_images": 10,
    }
    already_current_capabilities = {
        "text_to_video": True,
        "image_to_video": True,
        "multi_reference": True,
        "max_reference_images": 10,
        "custom_flag": "already-current",
    }
    model_rows = [
        (
            "doubao-seedance-2-0-mini-260615",
            "Seedance 2.0 Mini",
            "volcengine_ark",
            "ark",
            {"preview_cost": 25, "capabilities": old_capabilities},
        ),
        (
            "doubao-seedance-2-1-pro-270101",
            "Seedance 2.1 Pro",
            "custom_gateway",
            "ark",
            {"preview_cost": 35, "capabilities": {"custom_flag": "gateway-ark"}},
        ),
        (
            "doubao-seedance-3-0-pro-280101",
            "Seedance 3.0 Pro",
            "volcengine_ark",
            "ark",
            {"capabilities": already_current_capabilities},
        ),
        (
            "doubao-seedance-2-0-pro-unverified",
            "Unverified Seedance 2.0",
            "custom_openai",
            "openai",
            {"capabilities": old_capabilities},
        ),
        (
            "doubao-seedance-1-9-pro-legacy",
            "Seedance 1.9",
            "volcengine_ark",
            "ark",
            {"capabilities": old_capabilities},
        ),
    ]
    model_ids = {}
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        for sort_order, (model_id, display_name, provider, gateway_format, extra) in enumerate(
            model_rows,
            start=40,
        ):
            result = connection.execute(
                model_configs.insert().values(
                    use="video",
                    model_id=model_id,
                    display_name=display_name,
                    is_default=False,
                    sort_order=sort_order,
                    provider=provider,
                    gateway_format=gateway_format,
                    cost_credits=100,
                    unlock_cost=0,
                    enabled=True,
                    extra=extra,
                )
            )
            model_ids[model_id] = int(result.inserted_primary_key[0])

        for model_id, capabilities in (
            ("doubao-seedance-2-0-mini-260615", old_capabilities),
            ("doubao-seedance-3-0-pro-280101", already_current_capabilities),
        ):
            connection.execute(
                capability_versions.insert().values(
                    model_config_id=model_ids[model_id],
                    version=1,
                    schema_version="capability.v1",
                    capabilities=capabilities,
                    metadata_snapshot={
                        "schema_version": "model-catalog-metadata.v1",
                        "origin": "test",
                        "model_id": model_id,
                        "display_name": next(
                            row[1] for row in model_rows if row[0] == model_id
                        ),
                        "is_default": False,
                        "sort_order": next(
                            index for index, row in enumerate(model_rows, start=40)
                            if row[0] == model_id
                        ),
                        "enabled": True,
                    },
                    status="published",
                    is_active=True,
                    activated_at=now,
                )
            )

    command.upgrade(cfg, "0074_seedance_20_multi_reference")
    with engine.connect() as connection:
        extras = {
            row.model_id: row.extra
            for row in connection.execute(
                sa.select(model_configs.c.model_id, model_configs.c.extra).where(
                    model_configs.c.model_id.in_(list(model_ids))
                )
            )
        }
        version_rows = connection.execute(
            sa.select(
                capability_versions.c.model_config_id,
                capability_versions.c.version,
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.capabilities,
            )
            .where(capability_versions.c.model_config_id.in_(list(model_ids.values())))
            .order_by(
                capability_versions.c.model_config_id,
                capability_versions.c.version,
            )
        ).mappings().all()

    assert extras["doubao-seedance-2-0-mini-260615"] == {
        "preview_cost": 25,
        "capabilities": expected_capabilities,
    }
    assert extras["doubao-seedance-2-1-pro-270101"]["capabilities"] == {
        "custom_flag": "gateway-ark",
        "text_to_video": True,
        "image_to_video": True,
        "multi_reference": True,
        "max_reference_images": 10,
    }
    assert extras["doubao-seedance-3-0-pro-280101"]["capabilities"] == (
        already_current_capabilities
    )
    assert extras["doubao-seedance-2-0-pro-unverified"]["capabilities"] == (
        old_capabilities
    )
    assert extras["doubao-seedance-1-9-pro-legacy"]["capabilities"] == old_capabilities

    versions_by_model = {
        model_id: [
            row
            for row in version_rows
            if row["model_config_id"] == model_config_id
        ]
        for model_id, model_config_id in model_ids.items()
    }
    assert [
        (row["version"], row["status"], row["is_active"])
        for row in versions_by_model["doubao-seedance-2-0-mini-260615"]
    ] == [(1, "disabled", False), (2, "published", True)]
    assert versions_by_model["doubao-seedance-2-0-mini-260615"][-1][
        "capabilities"
    ] == expected_capabilities
    assert len(versions_by_model["doubao-seedance-2-1-pro-270101"]) == 1
    assert len(versions_by_model["doubao-seedance-3-0-pro-280101"]) == 1
    assert versions_by_model["doubao-seedance-2-0-pro-unverified"] == []
    assert versions_by_model["doubao-seedance-1-9-pro-legacy"] == []

    command.downgrade(cfg, "0073_seedance_15_multi_reference")
    command.upgrade(cfg, "0074_seedance_20_multi_reference")
    with engine.connect() as connection:
        version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id.in_(list(model_ids.values()))
            )
        )
    assert version_count == 4
    engine.dispose()


def test_0075_enables_grok_product_theme_and_detail_references_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "grok-video-product-references.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0074_seedance_20_multi_reference")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    model_id = "grok-imagine-video-1.5"
    old_capabilities = {
        "text_to_video": True,
        "image_to_video": True,
        "multi_reference": False,
        "custom_flag": "keep",
    }
    expected_capabilities = {
        **old_capabilities,
        "multi_reference": True,
        "max_reference_images": 2,
    }
    metadata_snapshot = {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "test",
        "model_id": model_id,
        "display_name": "Grok Video 1.5",
        "is_default": False,
        "sort_order": 50,
        "enabled": True,
    }
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        result = connection.execute(
            model_configs.insert().values(
                use="video",
                model_id=model_id,
                display_name="Grok Video 1.5",
                is_default=False,
                sort_order=50,
                provider="grok",
                gateway_format="openai",
                cost_credits=40,
                unlock_cost=0,
                enabled=True,
                extra={
                    "preview_cost": 20,
                    "submit_path": "/v1/videos/generations",
                    "capabilities": old_capabilities,
                },
            )
        )
        model_config_id = int(result.inserted_primary_key[0])
        connection.execute(
            capability_versions.insert().values(
                model_config_id=model_config_id,
                version=1,
                schema_version="capability.v1",
                capabilities=old_capabilities,
                metadata_snapshot=metadata_snapshot,
                status="published",
                is_active=True,
                activated_at=now,
            )
        )

    command.upgrade(cfg, "0075_grok_video_product_refs")
    with engine.connect() as connection:
        extra = connection.scalar(
            sa.select(model_configs.c.extra).where(model_configs.c.id == model_config_id)
        )
        versions = connection.execute(
            sa.select(
                capability_versions.c.version,
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.source_version_id,
                capability_versions.c.capabilities,
                capability_versions.c.metadata_snapshot,
            )
            .where(capability_versions.c.model_config_id == model_config_id)
            .order_by(capability_versions.c.version)
        ).mappings().all()

    assert extra == {
        "preview_cost": 20,
        "submit_path": "/v1/videos/generations",
        "product_images_field": "images",
        "product_images_item_field": "url",
        "capabilities": expected_capabilities,
    }
    assert [
        (row["version"], row["status"], row["is_active"])
        for row in versions
    ] == [(1, "disabled", False), (2, "published", True)]
    assert versions[1]["source_version_id"] is not None
    assert versions[1]["capabilities"] == expected_capabilities
    assert versions[1]["metadata_snapshot"] == metadata_snapshot

    command.downgrade(cfg, "0074_seedance_20_multi_reference")
    command.upgrade(cfg, "0075_grok_video_product_refs")
    with engine.connect() as connection:
        version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id == model_config_id
            )
        )
    assert version_count == 2
    engine.dispose()


def test_0076_replaces_grok_video_images_with_native_reference_schema(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "grok-video-native-reference-schema.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0075_grok_video_product_refs")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    old_extra = {
        "preview_cost": 20,
        "product_images_field": "images",
        "product_images_item_field": "url",
        "capabilities": {
            "text_to_video": True,
            "image_to_video": True,
            "multi_reference": True,
            "max_reference_images": 2,
        },
    }
    with engine.begin() as connection:
        result = connection.execute(
            model_configs.insert().values(
                use="video",
                model_id="grok-imagine-video-1.5",
                display_name="Grok Video 1.5",
                is_default=False,
                sort_order=50,
                provider="grok",
                gateway_format="openai",
                cost_credits=40,
                unlock_cost=0,
                enabled=True,
                extra=old_extra,
            )
        )
        model_config_id = int(result.inserted_primary_key[0])

    expected_extra = {
        "preview_cost": 20,
        "first_frame_field": "image",
        "first_frame_item_field": "url",
        "product_images_field": "reference_images",
        "product_images_item_field": "url",
        "negative_prompt_mode": "append_to_prompt",
        "capabilities": old_extra["capabilities"],
    }

    command.upgrade(cfg, "0076_grok_video_ref_schema")
    with engine.connect() as connection:
        extra = connection.scalar(
            sa.select(model_configs.c.extra).where(model_configs.c.id == model_config_id)
        )
    assert extra == expected_extra

    command.downgrade(cfg, "0075_grok_video_product_refs")
    with engine.connect() as connection:
        downgraded_extra = connection.scalar(
            sa.select(model_configs.c.extra).where(model_configs.c.id == model_config_id)
        )
    assert downgraded_extra == old_extra

    command.upgrade(cfg, "0076_grok_video_ref_schema")
    with engine.connect() as connection:
        upgraded_again = connection.scalar(
            sa.select(model_configs.c.extra).where(model_configs.c.id == model_config_id)
        )
    assert upgraded_again == expected_extra
    engine.dispose()


def test_0077_disables_seedance_15_reference_to_video_and_keeps_frames(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "seedance-15-frame-only.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0076_grok_video_ref_schema")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    model_id = "doubao-seedance-1-5-pro-251215"
    old_capabilities = {
        "text_to_video": True,
        "image_to_video": True,
        "first_last_frame": True,
        "multi_reference": True,
        "max_reference_images": 2,
        "custom_flag": "keep",
    }
    metadata_snapshot = {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "test",
        "model_id": model_id,
        "display_name": "Seedance 1.5 Pro",
        "is_default": False,
        "sort_order": 20,
        "enabled": True,
    }
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        result = connection.execute(
            model_configs.insert().values(
                use="video",
                model_id=model_id,
                display_name="Seedance 1.5 Pro",
                is_default=False,
                sort_order=20,
                provider="volcengine_ark",
                gateway_format="ark",
                cost_credits=100,
                unlock_cost=0,
                enabled=True,
                extra={
                    "preview_cost": 50,
                    "capabilities": old_capabilities,
                },
            )
        )
        model_config_id = int(result.inserted_primary_key[0])
        connection.execute(
            capability_versions.insert().values(
                model_config_id=model_config_id,
                version=1,
                schema_version="capability.v1",
                capabilities=old_capabilities,
                metadata_snapshot=metadata_snapshot,
                status="published",
                is_active=True,
                activated_at=now,
            )
        )

    command.upgrade(cfg, "0077_seedance_15_ref_fix")
    expected_capabilities = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_image": False,
        "first_last_frame": True,
        "multi_reference": False,
        "custom_flag": "keep",
    }
    with engine.connect() as connection:
        extra = connection.scalar(
            sa.select(model_configs.c.extra).where(model_configs.c.id == model_config_id)
        )
        versions = connection.execute(
            sa.select(
                capability_versions.c.version,
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.source_version_id,
                capability_versions.c.capabilities,
                capability_versions.c.metadata_snapshot,
            )
            .where(capability_versions.c.model_config_id == model_config_id)
            .order_by(capability_versions.c.version)
        ).mappings().all()

    assert extra == {
        "preview_cost": 50,
        "capabilities": expected_capabilities,
    }
    assert [
        (row["version"], row["status"], row["is_active"])
        for row in versions
    ] == [(1, "disabled", False), (2, "published", True)]
    assert versions[1]["source_version_id"] is not None
    assert versions[1]["capabilities"] == expected_capabilities
    assert versions[1]["metadata_snapshot"] == metadata_snapshot

    command.downgrade(cfg, "0076_grok_video_ref_schema")
    command.upgrade(cfg, "0077_seedance_15_ref_fix")
    with engine.connect() as connection:
        version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id == model_config_id
            )
        )
    assert version_count == 2
    engine.dispose()


def test_0078_splits_grok_reference_video_from_15_capabilities(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "grok-video-model-split.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0077_seedance_15_ref_fix")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    with engine.begin() as connection:
        for model_id in ("grok-imagine-video", "grok-imagine-video-1.5"):
            connection.execute(
                model_configs.insert().values(
                    use="video",
                    model_id=model_id,
                    display_name=model_id,
                    is_default=False,
                    sort_order=50,
                    provider="grok",
                    gateway_format="openai",
                    cost_credits=40,
                    unlock_cost=0,
                    enabled=True,
                    extra={
                        "capabilities": {
                            "text_to_video": True,
                            "image_to_video": model_id.endswith("1.5"),
                            "multi_reference": model_id.endswith("1.5"),
                            "max_reference_images": 2,
                        },
                        "product_images_field": "reference_images",
                        "product_images_item_field": "url",
                    },
                )
            )

    command.upgrade(cfg, "0078_grok_video_model_split")
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                model_configs.c.id,
                model_configs.c.model_id,
                model_configs.c.extra,
            ).where(model_configs.c.model_id.in_([
                "grok-imagine-video",
                "grok-imagine-video-1.5",
            ]))
        ).mappings().all()
        version_rows = connection.execute(
            sa.select(
                capability_versions.c.model_config_id,
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.capabilities,
            ).where(capability_versions.c.model_config_id.in_([
                int(row["id"]) for row in rows
            ]))
        ).mappings().all()

    by_model = {row["model_id"]: row for row in rows}
    reference_extra = by_model["grok-imagine-video"]["extra"]
    assert reference_extra["product_images_field"] == "reference_images"
    assert reference_extra["capabilities"] == {
        "text_to_video": True,
        "image_to_video": True,
        "reference_image": True,
        "multi_reference": True,
        "max_reference_images": 3,
    }

    grok_15_extra = by_model["grok-imagine-video-1.5"]["extra"]
    assert "product_images_field" not in grok_15_extra
    assert grok_15_extra["first_frame_field"] == "image"
    assert grok_15_extra["capabilities"] == {
        "text_to_video": True,
        "image_to_video": True,
        "reference_image": False,
        "multi_reference": False,
    }
    assert len(version_rows) == 2
    assert all(row["status"] == "published" for row in version_rows)
    assert all(row["is_active"] is True for row in version_rows)
    engine.dispose()


def test_0075_does_not_enable_unverified_grok_video_provider(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "unverified-grok-video-product-references.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0074_seedance_20_multi_reference")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    old_extra = {
        "capabilities": {
            "text_to_video": True,
            "image_to_video": True,
            "multi_reference": False,
        }
    }
    with engine.begin() as connection:
        result = connection.execute(
            model_configs.insert().values(
                use="video",
                model_id="grok-imagine-video-1.5",
                display_name="Unverified Grok Proxy",
                is_default=False,
                sort_order=60,
                provider="custom_openai",
                gateway_format="openai",
                cost_credits=40,
                unlock_cost=0,
                enabled=True,
                extra=old_extra,
            )
        )
        model_config_id = int(result.inserted_primary_key[0])

    command.upgrade(cfg, "0075_grok_video_product_refs")
    with engine.connect() as connection:
        extra = connection.scalar(
            sa.select(model_configs.c.extra).where(model_configs.c.id == model_config_id)
        )
        version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id == model_config_id
            )
        )

    assert extra == old_extra
    assert version_count == 0
    engine.dispose()


def test_0082_aligns_verified_model_capabilities_and_versions(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "verified-model-capability-matrix.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0081_refunds_soft_delete")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    specs = [
        (
            "image",
            "gpt-image-2",
            "yinyue",
            "openai",
            {
                "text_to_image": True,
                "image_to_image": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 2,
                "mask_edit": True,
            },
        ),
        (
            "image",
            "gemini-3.1-flash-image",
            "antigravity",
            "anthropic",
            {
                "text_to_image": True,
                "image_to_image": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 2,
                "mask_edit": False,
            },
        ),
        (
            "image",
            "grok-imagine-image",
            "grok",
            "openai",
            {
                "text_to_image": True,
                "image_to_image": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 2,
                "mask_edit": False,
            },
        ),
        (
            "video",
            "doubao-seedance-1-5-pro-251215",
            "volcengine_ark",
            "ark",
            {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": False,
                "first_last_frame": True,
                "multi_reference": False,
                "video_to_video": False,
            },
        ),
        (
            "video",
            "doubao-seedance-2-0-260128",
            "volcengine_ark",
            "ark",
            {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": True,
                "first_last_frame": True,
                "multi_reference": True,
                "max_reference_images": 9,
                "video_to_video": True,
            },
        ),
        (
            "video",
            "grok-imagine-video",
            "grok",
            "openai",
            {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 7,
                "first_last_frame": False,
                "video_to_video": True,
            },
        ),
        (
            "video",
            "grok-imagine-video-1.5",
            "grok",
            "openai",
            {
                "text_to_video": False,
                "image_to_video": True,
                "reference_image": False,
                "multi_reference": False,
                "first_last_frame": False,
                "video_to_video": False,
            },
        ),
        (
            "vision",
            "gpt-5.6-sol",
            None,
            "openai",
            {
                "image_analysis": True,
                "video_analysis": True,
                "product_profile": True,
                "portrait_profile": True,
            },
        ),
        (
            "vision",
            "gemini-3.1-pro-high",
            "antigravity",
            "anthropic",
            {
                "image_analysis": True,
                "video_analysis": True,
                "product_profile": True,
                "portrait_profile": True,
            },
        ),
        (
            "prompt",
            "claude-opus-4-6-thinking",
            "antigravity",
            "anthropic",
            {"prompt_optimization": True},
        ),
        (
            "prompt",
            "gemini-3.5-flash-low",
            "antigravity",
            "anthropic",
            {"prompt_optimization": True},
        ),
        (
            "prompt",
            "gemini-3.1-pro-high",
            "antigravity",
            "anthropic",
            {"prompt_optimization": True},
        ),
        (
            "prompt",
            "grok-4.5",
            "grok",
            "openai",
            {"prompt_optimization": True},
        ),
    ]
    model_ids: dict[tuple[str, str], int] = {}
    deleted_model_id: int | None = None
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        for use, model_id, _provider, _gateway_format, _expected in specs:
            existing_ids = list(
                connection.scalars(
                    sa.select(model_configs.c.id).where(
                        model_configs.c.use == use,
                        model_configs.c.model_id == model_id,
                    )
                )
            )
            if existing_ids:
                connection.execute(
                    sa.delete(capability_versions).where(
                        capability_versions.c.model_config_id.in_(existing_ids)
                    )
                )
            connection.execute(
                sa.delete(model_configs).where(
                    model_configs.c.use == use,
                    model_configs.c.model_id == model_id,
                )
            )
        for sort_order, (use, model_id, provider, gateway_format, _expected) in enumerate(
            specs,
            start=1,
        ):
            old_capabilities = {"custom_flag": "keep"}
            if use == "image":
                old_capabilities.update(
                    {
                        "image_edit": True,
                        "image_to_image": False,
                        "multi_reference": True,
                        "max_reference_images": 10,
                    }
                )
            elif use == "video":
                old_capabilities.update(
                    {
                        "image_to_video": False,
                        "reference_image": True,
                        "multi_reference": True,
                        "max_reference_images": 10,
                    }
                )
            elif use == "vision":
                old_capabilities.update(
                    {"image_analysis": False, "video_analysis": False}
                )
            else:
                old_capabilities["prompt_optimization"] = False
            extra = {"keep_extra": True, "capabilities": old_capabilities}
            if model_id == "grok-imagine-video-1.5":
                extra.update(
                    {
                        "product_images_field": "reference_images",
                        "product_images_item_field": "url",
                    }
                )
            result = connection.execute(
                model_configs.insert().values(
                    use=use,
                    model_id=model_id,
                    display_name=model_id,
                    is_default=False,
                    sort_order=sort_order,
                    provider=provider,
                    gateway_format=gateway_format,
                    cost_credits=5,
                    unlock_cost=0,
                    enabled=True,
                    extra=extra,
                )
            )
            model_config_id = int(result.inserted_primary_key[0])
            model_ids[(use, model_id)] = model_config_id
            connection.execute(
                capability_versions.insert().values(
                    model_config_id=model_config_id,
                    version=1,
                    schema_version="capability.v1",
                    capabilities=old_capabilities,
                    metadata_snapshot={
                        "schema_version": "model-catalog-metadata.v1",
                        "origin": "test",
                        "model_id": model_id,
                        "display_name": model_id,
                        "is_default": False,
                        "sort_order": sort_order,
                        "enabled": True,
                    },
                    status="published",
                    is_active=True,
                    activated_at=now,
                )
            )
        deleted_result = connection.execute(
            model_configs.insert().values(
                use="image",
                model_id="gpt-image-2",
                display_name="deleted-gpt-image-2",
                is_default=False,
                sort_order=999,
                provider="yinyue",
                gateway_format="openai",
                cost_credits=5,
                unlock_cost=0,
                enabled=False,
                extra={
                    "keep_deleted": True,
                    "capabilities": {"image_to_image": False},
                },
                deleted_at=now,
            )
        )
        deleted_model_id = int(deleted_result.inserted_primary_key[0])
        connection.execute(
            capability_versions.insert().values(
                model_config_id=deleted_model_id,
                version=1,
                schema_version="capability.v1",
                capabilities={"image_to_image": False},
                metadata_snapshot={
                    "schema_version": "model-catalog-metadata.v1",
                    "origin": "test",
                    "model_id": "gpt-image-2",
                    "display_name": "deleted-gpt-image-2",
                    "is_default": False,
                    "sort_order": 999,
                    "enabled": False,
                },
                status="published",
                is_active=True,
                activated_at=now,
            )
        )

    command.upgrade(cfg, "0082_model_capability_matrix")
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                model_configs.c.use,
                model_configs.c.model_id,
                model_configs.c.extra,
            ).where(model_configs.c.id.in_(list(model_ids.values())))
        ).mappings().all()
        versions = connection.execute(
            sa.select(
                capability_versions.c.model_config_id,
                capability_versions.c.version,
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.source_version_id,
                capability_versions.c.capabilities,
            )
            .where(capability_versions.c.model_config_id.in_(list(model_ids.values())))
            .order_by(
                capability_versions.c.model_config_id,
                capability_versions.c.version,
            )
        ).mappings().all()
        deleted_row = connection.execute(
            sa.select(model_configs.c.extra).where(
                model_configs.c.id == deleted_model_id
            )
        ).mappings().one()
        deleted_version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id == deleted_model_id
            )
        )

    rows_by_key = {(row["use"], row["model_id"]): row for row in rows}
    for use, model_id, _provider, _gateway_format, expected in specs:
        extra = rows_by_key[(use, model_id)]["extra"]
        assert extra["keep_extra"] is True
        assert extra["capabilities"] == {"custom_flag": "keep", **expected}
        assert "image_edit" not in extra["capabilities"]

    assert rows_by_key[("image", "gpt-image-2")]["extra"]["edit_path"] == (
        "/v1/images/edits"
    )
    assert rows_by_key[("video", "grok-imagine-video")]["extra"][
        "product_images_field"
    ] == "reference_images"
    grok_15_extra = rows_by_key[("video", "grok-imagine-video-1.5")]["extra"]
    assert "product_images_field" not in grok_15_extra
    assert grok_15_extra["first_frame_field"] == "image"

    versions_by_model: dict[int, list[dict]] = {}
    for version in versions:
        versions_by_model.setdefault(int(version["model_config_id"]), []).append(version)
    for use, model_id, _provider, _gateway_format, expected in specs:
        model_versions = versions_by_model[model_ids[(use, model_id)]]
        assert [
            (row["version"], row["status"], row["is_active"])
            for row in model_versions
        ] == [(1, "disabled", False), (2, "published", True)]
        assert model_versions[1]["source_version_id"] is not None
        assert model_versions[1]["capabilities"] == {"custom_flag": "keep", **expected}

    assert deleted_row["extra"] == {
        "keep_deleted": True,
        "capabilities": {"image_to_image": False},
    }
    assert deleted_version_count == 1

    command.downgrade(cfg, "0081_refunds_soft_delete")
    command.upgrade(cfg, "0082_model_capability_matrix")
    with engine.connect() as connection:
        version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id.in_(list(model_ids.values()))
            )
        )
    assert version_count == len(specs) * 2
    engine.dispose()


def test_0082_publishes_missing_version_for_unchanged_migration_model(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "unchanged-model-capability.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0081_refunds_soft_delete")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    with engine.connect() as connection:
        model_row = connection.execute(
            sa.select(model_configs).where(
                model_configs.c.use == "vision",
                model_configs.c.model_id == "gemini-3.1-pro-high",
            )
        ).mappings().one()
        before_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id == int(model_row["id"])
            )
        )
    assert model_row["extra"]["catalog_origin"] == "migration_0071"
    assert model_row["extra"]["capabilities"] == {
        "image_analysis": True,
        "video_analysis": True,
        "product_profile": True,
        "portrait_profile": True,
    }
    assert before_count == 0

    command.upgrade(cfg, "0082_model_capability_matrix")
    with engine.connect() as connection:
        published_versions = connection.execute(
            sa.select(
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.capabilities,
            ).where(
                capability_versions.c.model_config_id == int(model_row["id"])
            )
        ).mappings().all()
    assert published_versions == [
        {
            "status": "published",
            "is_active": True,
            "capabilities": model_row["extra"]["capabilities"],
        }
    ]

    command.downgrade(cfg, "0070_video_model_capabilities")
    with engine.connect() as connection:
        remaining = connection.scalar(
            sa.select(sa.func.count(model_configs.c.id)).where(
                model_configs.c.id == int(model_row["id"])
            )
        )
    assert remaining == 0
    engine.dispose()


def test_0082_does_not_assign_seedance_20_capabilities_to_future_versions(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "future-seedance-capabilities.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0081_refunds_soft_delete")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    future_model_ids = [
        "doubao-seedance-2-1-pro-270101",
        "doubao-seedance-3-0-pro-280101",
    ]
    inserted_ids: list[int] = []
    original_extra = {"capabilities": {"custom_flag": "manual-review"}}
    with engine.begin() as connection:
        for sort_order, model_id in enumerate(future_model_ids, start=901):
            result = connection.execute(
                model_configs.insert().values(
                    use="video",
                    model_id=model_id,
                    display_name=model_id,
                    is_default=False,
                    sort_order=sort_order,
                    provider="volcengine_ark",
                    gateway_format="ark",
                    cost_credits=100,
                    unlock_cost=0,
                    enabled=False,
                    extra=original_extra,
                )
            )
            inserted_ids.append(int(result.inserted_primary_key[0]))

    command.upgrade(cfg, "0082_model_capability_matrix")
    with engine.connect() as connection:
        extras = list(
            connection.scalars(
                sa.select(model_configs.c.extra).where(
                    model_configs.c.id.in_(inserted_ids)
                )
            )
        )
        version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id.in_(inserted_ids)
            )
        )

    assert extras == [original_extra, original_extra]
    assert version_count == 0
    engine.dispose()


@pytest.mark.parametrize(
    "row",
    [
        {
            "use": "video",
            "model_id": "doubao-seedance-2-0-260128",
            "provider": "volcengine_ark",
            "gateway_format": "openai",
        },
        {
            "use": "video",
            "model_id": "doubao-seedance-2-0-260128",
            "provider": "custom_openai",
            "gateway_format": "ark",
        },
        {
            "use": "vision",
            "model_id": "gemini-3.1-pro-high",
            "provider": "custom_openai",
            "gateway_format": "anthropic",
        },
        {
            "use": "prompt",
            "model_id": "gemini-3.1-pro-high",
            "provider": "antigravity",
            "gateway_format": "openai",
        },
    ],
)
def test_0082_requires_matching_provider_and_gateway_format(row):
    migration = _load_migration_module("0082_model_capability_matrix")

    assert migration._correction(row) is None


def test_0083_splits_video_input_semantics_and_preserves_version_history(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "video-input-capability-semantics.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0082_model_capability_matrix")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    specs = [
        (
            "doubao-seedance-1-5-pro-251215",
            "volcengine_ark",
            "ark",
            False,
            {
                "video_to_video": False,
                "video_reference": False,
                "video_edit": False,
                "audio_reference": False,
                "max_reference_videos": 0,
                "max_reference_audio": 0,
            },
        ),
        (
            "doubao-seedance-2-0-260128",
            "volcengine_ark",
            "ark",
            True,
            {
                "video_to_video": True,
                "video_reference": True,
                "video_edit": False,
                "audio_reference": False,
                "max_reference_videos": 1,
                "max_reference_audio": 0,
            },
        ),
        (
            "grok-imagine-video",
            "grok",
            "openai",
            False,
            {
                "video_to_video": True,
                "video_reference": False,
                "video_edit": True,
                "audio_reference": False,
                "max_reference_videos": 1,
                "max_reference_audio": 0,
            },
        ),
        (
            "grok-imagine-video-1.5",
            "grok",
            "openai",
            True,
            {
                "video_to_video": False,
                "video_reference": False,
                "video_edit": False,
                "audio_reference": False,
                "max_reference_videos": 0,
                "max_reference_audio": 0,
            },
        ),
    ]
    model_ids: dict[str, int] = {}
    old_version_ids: dict[str, int] = {}
    stale_capabilities: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        for model_id, _provider, _gateway_format, _enabled, _expected in specs:
            existing_ids = list(
                connection.scalars(
                    sa.select(model_configs.c.id).where(
                        model_configs.c.use == "video",
                        model_configs.c.model_id == model_id,
                    )
                )
            )
            if existing_ids:
                connection.execute(
                    sa.delete(capability_versions).where(
                        capability_versions.c.model_config_id.in_(existing_ids)
                    )
                )
            connection.execute(
                sa.delete(model_configs).where(
                    model_configs.c.use == "video",
                    model_configs.c.model_id == model_id,
                )
            )

        for sort_order, (
            model_id,
            provider,
            gateway_format,
            enabled,
            expected,
        ) in enumerate(specs, start=801):
            stale = {
                "custom_flag": "keep",
                "text_to_video": True,
                "video_to_video": not expected["video_to_video"],
                "video_reference": not expected["video_reference"],
                "video_edit": not expected["video_edit"],
                "audio_reference": True,
                "max_reference_videos": 99,
                "max_reference_audio": 99,
            }
            stale_capabilities[model_id] = stale
            result = connection.execute(
                model_configs.insert().values(
                    use="video",
                    model_id=model_id,
                    display_name=model_id,
                    is_default=False,
                    sort_order=sort_order,
                    provider=provider,
                    gateway_format=gateway_format,
                    cost_credits=20,
                    unlock_cost=0,
                    enabled=enabled,
                    extra={
                        "keep_extra": f"extra:{model_id}",
                        "capabilities": stale,
                    },
                )
            )
            model_config_id = int(result.inserted_primary_key[0])
            model_ids[model_id] = model_config_id
            version_result = connection.execute(
                capability_versions.insert().values(
                    model_config_id=model_config_id,
                    version=4,
                    schema_version="capability.v1",
                    capabilities=stale,
                    metadata_snapshot={
                        "schema_version": "model-catalog-metadata.v1",
                        "origin": "test_0083",
                        "model_id": model_id,
                        "display_name": model_id,
                        "is_default": False,
                        "sort_order": sort_order,
                        "enabled": enabled,
                    },
                    status="published",
                    is_active=True,
                    activated_at=now,
                )
            )
            old_version_ids[model_id] = int(version_result.inserted_primary_key[0])

        deleted_extra = {
            "keep_deleted": True,
            "capabilities": {"video_reference": False},
        }
        deleted_result = connection.execute(
            model_configs.insert().values(
                use="video",
                model_id="doubao-seedance-2-0-260128",
                display_name="deleted-seedance-2.0",
                is_default=False,
                sort_order=899,
                provider="volcengine_ark",
                gateway_format="ark",
                cost_credits=20,
                unlock_cost=0,
                enabled=False,
                extra=deleted_extra,
                deleted_at=now,
            )
        )
        deleted_model_id = int(deleted_result.inserted_primary_key[0])
        connection.execute(
            capability_versions.insert().values(
                model_config_id=deleted_model_id,
                version=1,
                schema_version="capability.v1",
                capabilities=deleted_extra["capabilities"],
                metadata_snapshot={
                    "schema_version": "model-catalog-metadata.v1",
                    "origin": "test_0083_deleted",
                    "model_id": "doubao-seedance-2-0-260128",
                    "display_name": "deleted-seedance-2.0",
                    "is_default": False,
                    "sort_order": 899,
                    "enabled": False,
                },
                status="published",
                is_active=True,
                activated_at=now,
            )
        )

        ignored_specs = [
            (
                "doubao-seedance-2-1-pro-270101",
                "volcengine_ark",
                "ark",
            ),
            (
                "doubao-seedance-2-0-unverified-260128",
                "custom_openai",
                "ark",
            ),
        ]
        ignored_model_ids = []
        for sort_order, (model_id, provider, gateway_format) in enumerate(
            ignored_specs,
            start=901,
        ):
            result = connection.execute(
                model_configs.insert().values(
                    use="video",
                    model_id=model_id,
                    display_name=model_id,
                    is_default=False,
                    sort_order=sort_order,
                    provider=provider,
                    gateway_format=gateway_format,
                    cost_credits=20,
                    unlock_cost=0,
                    enabled=False,
                    extra={"capabilities": {"custom_flag": "manual-review"}},
                )
            )
            ignored_model_ids.append(int(result.inserted_primary_key[0]))

    command.upgrade(cfg, "0083_video_input_semantics")
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                model_configs.c.id,
                model_configs.c.model_id,
                model_configs.c.enabled,
                model_configs.c.extra,
            ).where(model_configs.c.id.in_(list(model_ids.values())))
        ).mappings().all()
        versions = connection.execute(
            sa.select(
                capability_versions.c.id,
                capability_versions.c.model_config_id,
                capability_versions.c.version,
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.source_version_id,
                capability_versions.c.capabilities,
                capability_versions.c.metadata_snapshot,
                capability_versions.c.disabled_at,
            )
            .where(capability_versions.c.model_config_id.in_(list(model_ids.values())))
            .order_by(
                capability_versions.c.model_config_id,
                capability_versions.c.version,
            )
        ).mappings().all()
        deleted_row = connection.execute(
            sa.select(model_configs.c.extra).where(
                model_configs.c.id == deleted_model_id
            )
        ).mappings().one()
        deleted_version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id == deleted_model_id
            )
        )
        ignored_extras = list(
            connection.scalars(
                sa.select(model_configs.c.extra)
                .where(model_configs.c.id.in_(ignored_model_ids))
                .order_by(model_configs.c.id)
            )
        )
        ignored_version_count = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id.in_(ignored_model_ids)
            )
        )

    rows_by_model = {row["model_id"]: row for row in rows}
    versions_by_model: dict[int, list[dict]] = {}
    for version in versions:
        versions_by_model.setdefault(int(version["model_config_id"]), []).append(version)

    for model_id, _provider, _gateway_format, enabled, expected in specs:
        row = rows_by_model[model_id]
        assert row["enabled"] is enabled
        assert row["extra"]["keep_extra"] == f"extra:{model_id}"
        assert row["extra"]["capabilities"] == {
            "custom_flag": "keep",
            "text_to_video": True,
            **expected,
        }
        model_versions = versions_by_model[model_ids[model_id]]
        assert [
            (version["version"], version["status"], version["is_active"])
            for version in model_versions
        ] == [(4, "disabled", False), (5, "published", True)]
        old_version, new_version = model_versions
        assert old_version["id"] == old_version_ids[model_id]
        assert old_version["capabilities"] == stale_capabilities[model_id]
        assert old_version["disabled_at"] is not None
        assert new_version["source_version_id"] == old_version_ids[model_id]
        assert new_version["capabilities"] == {
            "custom_flag": "keep",
            "text_to_video": True,
            **expected,
        }
        assert new_version["metadata_snapshot"]["origin"] == "test_0083"

    assert deleted_row["extra"] == deleted_extra
    assert deleted_version_count == 1
    assert ignored_extras == [
        {"capabilities": {"custom_flag": "manual-review"}},
        {"capabilities": {"custom_flag": "manual-review"}},
    ]
    assert ignored_version_count == 0

    command.downgrade(cfg, "0082_model_capability_matrix")
    command.upgrade(cfg, "0083_video_input_semantics")
    with engine.connect() as connection:
        version_count_after_repeat = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id.in_(list(model_ids.values()))
            )
        )
    assert version_count_after_repeat == len(specs) * 2
    engine.dispose()

@pytest.mark.parametrize(
    "row",
    [
        {
            "use": "video",
            "model_id": "doubao-seedance-2-1-pro-270101",
            "provider": "volcengine_ark",
            "gateway_format": "ark",
        },
        {
            "use": "video",
            "model_id": "doubao-seedance-2-0-260128",
            "provider": "custom_openai",
            "gateway_format": "ark",
        },
        {
            "use": "video",
            "model_id": "doubao-seedance-2-0-260128",
            "provider": "volcengine_ark",
            "gateway_format": "openai",
        },
        {
            "use": "video",
            "model_id": "grok-imagine-video",
            "provider": "custom_openai",
            "gateway_format": "openai",
        },
        {
            "use": "video",
            "model_id": "grok-imagine-video",
            "provider": "grok",
            "gateway_format": "ark",
        },
        {
            "use": "vision",
            "model_id": "grok-imagine-video-1.5",
            "provider": "grok",
            "gateway_format": "openai",
        },
    ],
)
def test_0083_requires_matching_model_provider_and_gateway_format(row):
    migration = _load_migration_module("0083_video_input_capability_semantics")

    assert migration._correction(row) is None


@pytest.mark.parametrize(
    ("model_id", "provider", "gateway_format", "expected"),
    [
        (
            "doubao-seedance-1-5-pro-251215",
            "volcengine_ark",
            "ark",
            {
                "durations": list(range(4, 13)),
                "generated_audio": True,
                "generated_audio_configurable": False,
                "max_duration_seconds": 12,
                "min_duration_seconds": 4,
                "resolutions": ["480p", "720p", "1080p"],
            },
        ),
        (
            "doubao-seedance-2-0-260128",
            "volcengine_ark",
            "ark",
            {
                "durations": list(range(4, 16)),
                "frame_reference_mode_exclusive": True,
                "generated_audio": True,
                "generated_audio_configurable": False,
                "max_duration_seconds": 15,
                "min_duration_seconds": 4,
                "resolutions": ["480p", "720p", "1080p"],
            },
        ),
        (
            "doubao-seedance-2-0-fast-260128",
            "volcengine_ark",
            "ark",
            {
                "durations": list(range(4, 16)),
                "frame_reference_mode_exclusive": True,
                "generated_audio": True,
                "generated_audio_configurable": False,
                "max_duration_seconds": 15,
                "min_duration_seconds": 4,
                "resolutions": ["480p", "720p"],
            },
        ),
        (
            "doubao-seedance-2-0-mini-260615",
            "volcengine_ark",
            "ark",
            {
                "durations": list(range(4, 16)),
                "frame_reference_mode_exclusive": True,
                "generated_audio": True,
                "generated_audio_configurable": False,
                "max_duration_seconds": 15,
                "min_duration_seconds": 4,
                "resolutions": ["480p", "720p"],
            },
        ),
        (
            "grok-imagine-video",
            "grok",
            "openai",
            {
                "durations": list(range(1, 16)),
                "max_duration_seconds": 15,
                "max_reference_duration_seconds": 10,
                "min_duration_seconds": 1,
                "reference_image_mode_exclusive": True,
                "resolutions": ["480p", "720p"],
            },
        ),
        (
            "grok-imagine-video-1.5",
            "grok",
            "openai",
            {
                "durations": list(range(1, 16)),
                "max_duration_seconds": 15,
                "min_duration_seconds": 1,
                "resolutions": ["480p", "720p", "1080p"],
            },
        ),
    ],
)
def test_0084_declares_verified_video_mode_constraints(
    model_id,
    provider,
    gateway_format,
    expected,
):
    migration = _load_migration_module("0084_video_mode_constraints")

    assert migration._correction(
        {
            "use": "video",
            "model_id": model_id,
            "provider": provider,
            "gateway_format": gateway_format,
        }
    ) == expected


@pytest.mark.parametrize(
    "row",
    [
        {
            "use": "image",
            "model_id": "grok-imagine-video",
            "provider": "grok",
            "gateway_format": "openai",
        },
        {
            "use": "video",
            "model_id": "grok-imagine-video",
            "provider": "custom_openai",
            "gateway_format": "openai",
        },
        {
            "use": "video",
            "model_id": "doubao-seedance-2-0-260128",
            "provider": "volcengine_ark",
            "gateway_format": "openai",
        },
        {
            "use": "video",
            "model_id": "doubao-seedance-2-1-pro-270101",
            "provider": "volcengine_ark",
            "gateway_format": "ark",
        },
    ],
)
def test_0084_requires_matching_model_provider_and_gateway_format(row):
    migration = _load_migration_module("0084_video_mode_constraints")

    assert migration._correction(row) is None


def test_0084_publishes_immutable_capability_versions_idempotently(
    tmp_path,
    monkeypatch,
):
    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "video-mode-constraints.db"
    database_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0083_video_input_semantics")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=engine)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=engine,
    )
    specs = [
        (
            "doubao-seedance-1-5-pro-251215",
            "volcengine_ark",
            "ark",
            {
                "durations": list(range(4, 13)),
                "generated_audio": True,
                "generated_audio_configurable": False,
                "max_duration_seconds": 12,
                "min_duration_seconds": 4,
                "resolutions": ["480p", "720p", "1080p"],
            },
        ),
        (
            "doubao-seedance-2-0-260128",
            "volcengine_ark",
            "ark",
            {
                "durations": list(range(4, 16)),
                "frame_reference_mode_exclusive": True,
                "generated_audio": True,
                "generated_audio_configurable": False,
                "max_duration_seconds": 15,
                "min_duration_seconds": 4,
                "resolutions": ["480p", "720p", "1080p"],
            },
        ),
        (
            "doubao-seedance-2-0-fast-260128",
            "volcengine_ark",
            "ark",
            {
                "durations": list(range(4, 16)),
                "frame_reference_mode_exclusive": True,
                "generated_audio": True,
                "generated_audio_configurable": False,
                "max_duration_seconds": 15,
                "min_duration_seconds": 4,
                "resolutions": ["480p", "720p"],
            },
        ),
        (
            "doubao-seedance-2-0-mini-260615",
            "volcengine_ark",
            "ark",
            {
                "durations": list(range(4, 16)),
                "frame_reference_mode_exclusive": True,
                "generated_audio": True,
                "generated_audio_configurable": False,
                "max_duration_seconds": 15,
                "min_duration_seconds": 4,
                "resolutions": ["480p", "720p"],
            },
        ),
        (
            "grok-imagine-video",
            "grok",
            "openai",
            {
                "durations": list(range(1, 16)),
                "max_duration_seconds": 15,
                "max_reference_duration_seconds": 10,
                "min_duration_seconds": 1,
                "reference_image_mode_exclusive": True,
                "resolutions": ["480p", "720p"],
            },
        ),
        (
            "grok-imagine-video-1.5",
            "grok",
            "openai",
            {
                "durations": list(range(1, 16)),
                "max_duration_seconds": 15,
                "min_duration_seconds": 1,
                "resolutions": ["480p", "720p", "1080p"],
            },
        ),
    ]
    model_ids = {}
    old_version_ids = {}
    stale_capabilities = {
        "custom_flag": "keep",
        "durations": [5],
        "generated_audio_configurable": True,
        "max_duration_seconds": 99,
        "min_duration_seconds": 2,
        "resolutions": ["4k"],
    }
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        for model_id, _provider, _gateway_format, _expected in specs:
            existing_ids = list(
                connection.scalars(
                    sa.select(model_configs.c.id).where(
                        model_configs.c.use == "video",
                        model_configs.c.model_id == model_id,
                    )
                )
            )
            if existing_ids:
                connection.execute(
                    sa.delete(capability_versions).where(
                        capability_versions.c.model_config_id.in_(existing_ids)
                    )
                )
                connection.execute(
                    sa.delete(model_configs).where(model_configs.c.id.in_(existing_ids))
                )
        for sort_order, (model_id, provider, gateway_format, _expected) in enumerate(
            specs,
            start=1,
        ):
            result = connection.execute(
                model_configs.insert().values(
                    use="video",
                    model_id=model_id,
                    display_name=model_id,
                    is_default=False,
                    sort_order=sort_order,
                    provider=provider,
                    gateway_format=gateway_format,
                    cost_credits=10,
                    unlock_cost=0,
                    enabled=True,
                    extra={
                        "keep_extra": f"extra:{model_id}",
                        "capabilities": dict(stale_capabilities),
                    },
                )
            )
            model_config_id = int(result.inserted_primary_key[0])
            model_ids[model_id] = model_config_id
            version_result = connection.execute(
                capability_versions.insert().values(
                    model_config_id=model_config_id,
                    version=4,
                    schema_version="capability.v1",
                    capabilities=dict(stale_capabilities),
                    metadata_snapshot={
                        "schema_version": "model-catalog-metadata.v1",
                        "origin": "test_0084",
                        "model_id": model_id,
                        "display_name": model_id,
                        "is_default": False,
                        "sort_order": sort_order,
                        "enabled": True,
                    },
                    status="published",
                    is_active=True,
                    activated_at=now,
                )
            )
            old_version_ids[model_id] = int(version_result.inserted_primary_key[0])

    command.upgrade(cfg, "0084_video_mode_constraints")
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(model_configs.c.model_id, model_configs.c.extra).where(
                model_configs.c.id.in_(list(model_ids.values()))
            )
        ).mappings().all()
        versions = connection.execute(
            sa.select(
                capability_versions.c.id,
                capability_versions.c.model_config_id,
                capability_versions.c.version,
                capability_versions.c.status,
                capability_versions.c.is_active,
                capability_versions.c.source_version_id,
                capability_versions.c.capabilities,
                capability_versions.c.metadata_snapshot,
                capability_versions.c.disabled_at,
            )
            .where(capability_versions.c.model_config_id.in_(list(model_ids.values())))
            .order_by(
                capability_versions.c.model_config_id,
                capability_versions.c.version,
            )
        ).mappings().all()

    rows_by_model = {row["model_id"]: row for row in rows}
    versions_by_model = {}
    for version in versions:
        versions_by_model.setdefault(int(version["model_config_id"]), []).append(version)
    for model_id, _provider, _gateway_format, expected in specs:
        capabilities = rows_by_model[model_id]["extra"]["capabilities"]
        assert capabilities == {"custom_flag": "keep", **expected}
        assert rows_by_model[model_id]["extra"]["keep_extra"] == f"extra:{model_id}"
        model_versions = versions_by_model[model_ids[model_id]]
        assert [
            (version["version"], version["status"], version["is_active"])
            for version in model_versions
        ] == [(4, "disabled", False), (5, "published", True)]
        old_version, new_version = model_versions
        assert old_version["id"] == old_version_ids[model_id]
        assert old_version["capabilities"] == stale_capabilities
        assert old_version["disabled_at"] is not None
        assert new_version["source_version_id"] == old_version_ids[model_id]
        assert new_version["capabilities"] == {"custom_flag": "keep", **expected}
        assert new_version["metadata_snapshot"]["origin"] == "test_0084"

    command.downgrade(cfg, "0083_video_input_semantics")
    command.upgrade(cfg, "0084_video_mode_constraints")
    with engine.connect() as connection:
        version_count_after_repeat = connection.scalar(
            sa.select(sa.func.count(capability_versions.c.id)).where(
                capability_versions.c.model_config_id.in_(list(model_ids.values()))
            )
        )
    assert version_count_after_repeat == len(specs) * 2
    engine.dispose()
