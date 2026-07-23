"""Shared base for ORM models: imports, portable types, helper defaults.

Everything imported here is re-exported for domain submodules to use.
"""
from __future__ import annotations

import hashlib  # noqa: F401
import json  # noqa: F401
from datetime import datetime  # noqa: F401
from typing import Any  # noqa: F401

from sqlalchemy import (  # noqa: F401
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB  # noqa: F401
from sqlalchemy.orm import Mapped, mapped_column  # noqa: F401

from ..db import Base  # noqa: F401

# Portable types: JSONB / BIGINT on PostgreSQL (production), JSON / INTEGER on
# SQLite (so the same models run in lightweight tests / quick local trials).
JSONType = JSON().with_variant(JSONB(), "postgresql")
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def _default_workflow_compiled_snapshot(context: Any) -> dict:
    """Keep legacy ORM fixtures insertable while marking their provenance."""
    parameters = context.get_current_parameters()
    workflow = parameters.get("workflow_snapshot")
    if not isinstance(workflow, dict):
        workflow = {}
    return {
        "schema_version": "workflow-compiled.v1",
        "legacy_direct_construct": True,
        "dag": {
            "schema_version": parameters.get("workflow_schema_version"),
            "workflow": workflow,
        },
    }


def _default_workflow_compiled_snapshot_hash(context: Any) -> str:
    parameters = context.get_current_parameters()
    snapshot = parameters.get("compiled_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = _default_workflow_compiled_snapshot(context)
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
