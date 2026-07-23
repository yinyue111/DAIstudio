"""SQLite foreign-key parity with the PostgreSQL runtime."""
from __future__ import annotations

import warnings
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, SAWarning

from app.db import SessionLocal, configure_sqlite_foreign_keys, engine
from app.models import Base, GenerationDispatch, GenTask, ModelConfig
from app.services.model_versions import sync_model_versions


def test_sqlite_connections_enforce_foreign_keys(client):
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_sqlite_helper_configures_test_created_engines():
    test_engine = create_engine("sqlite://")
    configure_sqlite_foreign_keys(test_engine)
    try:
        with test_engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        test_engine.dispose()


def test_metadata_table_order_has_no_foreign_key_cycles():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SAWarning)
        tables = Base.metadata.sorted_tables

    assert tables
    cycle_warnings = [
        item for item in caught if "unresolvable cycles" in str(item.message).lower()
    ]
    assert cycle_warnings == []


def test_sqlite_rejects_orphan_generation_dispatch(client):
    with SessionLocal() as db:
        db.add(
            GenerationDispatch(
                task_id=9_999_999_999,
                attempt=1,
                task_name="generate.image",
                celery_task_id=f"orphan-{uuid4().hex}",
                status="pending",
                publish_attempts=0,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_deleting_generation_task_cascades_dispatches(client, make_user):
    user_id = make_user("13999079991")
    with SessionLocal() as db:
        task = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            prompt={"final_text": "foreign key cascade"},
            params={},
            status="queued",
            cost_frozen=0,
            cost_settled=0,
        )
        db.add(task)
        db.flush()
        dispatch = GenerationDispatch(
            task_id=int(task.id),
            attempt=1,
            task_name="generate.image",
            celery_task_id=f"cascade-{uuid4().hex}",
            status="pending",
            publish_attempts=0,
        )
        db.add(dispatch)
        db.commit()
        dispatch_id = int(dispatch.id)

        db.delete(task)
        db.commit()

        assert db.get(GenerationDispatch, dispatch_id) is None


def test_deleting_model_config_cascades_capability_and_price_versions(client):
    with SessionLocal() as db:
        model = ModelConfig(
            use="image",
            model_id=f"foreign-key-cascade-{uuid4().hex}",
            display_name="Foreign Key Cascade",
            is_default=False,
            sort_order=999,
            provider="openai",
            cost_credits=3,
            unlock_cost=1,
            enabled=True,
            extra={"capabilities": {"text_to_image": True}},
        )
        db.add(model)
        capability, price = sync_model_versions(db, model)
        db.commit()
        capability_id = int(capability.id)
        price_id = int(price.id)

        db.delete(model)
        db.commit()

        assert db.get(type(capability), capability_id) is None
        assert db.get(type(price), price_id) is None
