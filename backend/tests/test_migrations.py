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
