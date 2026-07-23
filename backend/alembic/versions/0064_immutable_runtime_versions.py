"""add immutable route/tool versions and compiled workflow snapshots

Revision ID: 0064_immutable_runtime_versions
Revises: 0063_model_version_lifecycle
Create Date: 2026-07-19
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "0064_immutable_runtime_versions"
down_revision = "0063_model_version_lifecycle"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _route_config(row: dict) -> dict:
    return {
        "name": row["name"],
        "model_id": row["model_id"],
        "provider": row["provider"],
        "base_url": row["base_url"],
        "api_key_encrypted": row["api_key_encrypted"],
        "gateway_format": row["gateway_format"],
        "extra": row["extra"] or {},
        "priority": int(row["priority"] or 0),
        "enabled": bool(row["enabled"]),
        "managed_by_model_config": bool(row["managed_by_model_config"]),
        "failure_threshold": int(row["failure_threshold"] or 1),
        "window_seconds": int(row["window_seconds"] or 1),
        "cooldown_seconds": int(row["cooldown_seconds"] or 1),
    }


def _canonical_hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


_REFERENCE_FIELDS = {
    "model_config_id": "model_config_ids",
    "capability_version_id": "capability_version_ids",
    "price_version_id": "price_version_ids",
    "route_id": "route_ids",
    "route_version_id": "route_version_ids",
}


def _runtime_references(*values: Any) -> dict[str, list[int]]:
    references = {target: set() for target in _REFERENCE_FIELDS.values()}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                target = _REFERENCE_FIELDS.get(str(key))
                if target is not None and item is not None and not isinstance(item, bool):
                    try:
                        references[target].add(int(item))
                    except (TypeError, ValueError):
                        pass
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    return {key: sorted(items) for key, items in references.items()}


def _topological_order(nodes: list[dict]) -> list[str]:
    declared = [str(node.get("key") or "") for node in nodes]
    declared = [key for key in declared if key]
    dependencies = {
        str(node.get("key") or ""): [
            str(item) for item in (node.get("depends_on") or []) if str(item)
        ]
        for node in nodes
        if str(node.get("key") or "")
    }
    pending = list(declared)
    resolved: list[str] = []
    while pending:
        ready = [key for key in pending if all(item in resolved for item in dependencies[key])]
        if not ready:
            return declared
        for key in ready:
            resolved.append(key)
            pending.remove(key)
    return resolved


def _legacy_compiled_snapshot(
    workflow: dict,
    tool_run: dict | None,
    tool_definition: dict | None,
    tool_version: dict | None,
    quote: dict | None,
    project_id: int | None,
) -> dict:
    graph = workflow or {}
    raw_nodes = graph.get("nodes") if isinstance(graph, dict) else []
    raw_nodes = [node for node in raw_nodes if isinstance(node, dict)]
    declared_order = [str(node.get("key")) for node in raw_nodes if node.get("key")]
    topological_order = _topological_order(raw_nodes)
    topological_indexes = {key: index for index, key in enumerate(topological_order)}
    nodes = [
        {
            "key": str(node.get("key")),
            "type": node.get("type"),
            "declared_index": index,
            "topological_index": topological_indexes.get(str(node.get("key")), index),
            "depends_on": list(node.get("depends_on") or []),
            "config": dict(node.get("config") or {}),
            "max_attempts": int(node.get("max_attempts") or 3),
            "side_effect": bool(node.get("side_effect", False)),
            "compensation": node.get("compensation"),
        }
        for index, node in enumerate(raw_nodes)
        if node.get("key")
    ]
    capabilities = dict((tool_version or {}).get("capabilities") or {})
    tool_snapshot = {
        "definition": {
            "id": (tool_definition or {}).get("id"),
            "slug": (tool_definition or {}).get("slug"),
            "name": (tool_definition or {}).get("name"),
            "description": (tool_definition or {}).get("description"),
            "category": (tool_definition or {}).get("category"),
            "renderer": (tool_definition or {}).get("renderer"),
            "entry_path": (tool_definition or {}).get("entry_path"),
            "icon": (tool_definition or {}).get("icon"),
            "sort_order": int((tool_definition or {}).get("sort_order") or 0),
            "enabled": bool((tool_definition or {}).get("enabled", False)),
            "featured": bool((tool_definition or {}).get("featured", False)),
            "created_at": _iso((tool_definition or {}).get("created_at")),
            "updated_at": _iso((tool_definition or {}).get("updated_at")),
        },
        "version": {
            "id": (tool_version or {}).get("id"),
            "tool_definition_id": (tool_version or {}).get("tool_definition_id"),
            "version": (tool_version or {}).get("version"),
            "schema_version": (tool_version or {}).get("schema_version"),
            "input_schema": dict((tool_version or {}).get("input_schema") or {}),
            "workflow": dict((tool_version or {}).get("workflow") or {}),
            "pricing_policy": dict((tool_version or {}).get("pricing_policy") or {}),
            "capabilities": capabilities,
            "status": (tool_version or {}).get("status"),
            "is_active": bool((tool_version or {}).get("is_active", False)),
            "source_version_id": (tool_version or {}).get("source_version_id"),
            "activated_at": _iso((tool_version or {}).get("activated_at")),
            "disabled_at": _iso((tool_version or {}).get("disabled_at")),
            "retired_at": _iso((tool_version or {}).get("retired_at")),
            "created_at": _iso((tool_version or {}).get("created_at")),
            "updated_at": _iso((tool_version or {}).get("updated_at")),
        },
    }
    model_snapshot = (quote or {}).get("model_snapshot") or {}
    quote_snapshot = {
        "id": (quote or {}).get("id"),
        "user_id": (quote or {}).get("user_id"),
        "kind": (quote or {}).get("kind"),
        "status": (quote or {}).get("status"),
        "category": (quote or {}).get("category"),
        "stage": (quote or {}).get("stage"),
        "client_request_id": (quote or {}).get("client_request_id"),
        "request_fingerprint": (quote or {}).get("request_fingerprint"),
        "model_config_id": (quote or {}).get("model_config_id"),
        "capability_version_id": (quote or {}).get("capability_version_id"),
        "price_version_id": (quote or {}).get("price_version_id"),
        "tool_version_id": (quote or {}).get("tool_version_id"),
        "request_snapshot": dict((quote or {}).get("request_snapshot") or {}),
        "model_snapshot": dict(model_snapshot) if isinstance(model_snapshot, dict) else {},
        "pricing_snapshot": dict((quote or {}).get("pricing_snapshot") or {}),
        "price_breakdown": dict((quote or {}).get("price_breakdown") or {}),
        "subject_snapshot": dict((quote or {}).get("subject_snapshot") or {}),
        "warnings": list((quote or {}).get("warnings") or []),
        "estimated_credits": int((quote or {}).get("estimated_credits") or 0),
        "expires_at": _iso((quote or {}).get("expires_at")),
        "created_at": _iso((quote or {}).get("created_at")),
    }
    node_safety = {
        node["key"]: dict(node["config"].get("safety_policy") or {})
        for node in nodes
        if isinstance(node["config"].get("safety_policy"), dict)
    }
    return {
        "schema_version": "workflow-compiled.v1",
        "legacy_backfill": True,
        "tool": tool_snapshot,
        "input": {
            "project_id": int(project_id) if project_id is not None else None,
            "client_request_id": (tool_run or {}).get("client_request_id"),
            "request_fingerprint": (tool_run or {}).get("request_fingerprint"),
            "payload": dict((tool_run or {}).get("input_snapshot") or {}),
        },
        "dag": {
            "schema_version": graph.get("type"),
            "declared_order": declared_order,
            "topological_order": topological_order,
            "output_node": graph.get("output_node"),
            "studio_preset": graph.get("studio_preset"),
            "nodes": nodes,
        },
        "runtime_references": _runtime_references(quote_snapshot, tool_snapshot["version"]),
        "model_binding": {
            "model_config_id": (quote or {}).get("model_config_id"),
            "capability_version_id": (quote or {}).get("capability_version_id"),
            "price_version_id": (quote or {}).get("price_version_id"),
            "model_snapshot": dict(model_snapshot) if isinstance(model_snapshot, dict) else {},
            "route_snapshot": (
                model_snapshot.get("route_snapshot") if isinstance(model_snapshot, dict) else None
            ),
        },
        "quote_binding": quote_snapshot,
        "pricing_binding": {
            "tool_policy": dict((tool_version or {}).get("pricing_policy") or {}),
            "quote_snapshot": dict((quote or {}).get("pricing_snapshot") or {}),
            "price_breakdown": dict((quote or {}).get("price_breakdown") or {}),
            "estimated_credits": int((quote or {}).get("estimated_credits") or 0),
        },
        "safety_policy": {
            "declared": dict(capabilities.get("safety_policy") or {}),
            "tool_capabilities": capabilities,
            "node_overrides": node_safety,
        },
        "warnings": list((quote or {}).get("warnings") or []),
        "retry_policy": {
            "default_lease_seconds": 300,
            "nodes": [
                {"key": node["key"], "max_attempts": node["max_attempts"]}
                for node in nodes
            ],
        },
        "failure_policy": {
            "terminal_status": "failed",
            "cancel_unstarted_nodes": True,
            "refund_unsettled_reservation": True,
        },
        "compensation_policy": {
            "execution_order": "reverse_topological",
            "trigger_on": ["failure", "cancellation"],
            "nodes": [
                {
                    "key": node["key"],
                    "side_effect": node["side_effect"],
                    "compensation": node["compensation"],
                }
                for node in nodes
                if node["side_effect"] or node["compensation"] is not None
            ],
        },
    }


_ROUTE_VERSION_COLUMNS = {
    "id",
    "route_id",
    "version",
    "schema_version",
    "config_snapshot",
    "status",
    "is_active",
    "source_version_id",
    "activated_at",
    "disabled_at",
    "retired_at",
    "created_at",
    "updated_at",
}
_ROUTE_VERSION_CHECKS = {
    "ck_model_route_versions_version_positive",
    "ck_model_route_versions_status_valid",
    "ck_model_route_versions_active_status",
}


def _create_model_route_versions() -> None:
    op.create_table(
        "model_route_versions",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "route_id",
            sa.BigInteger(),
            sa.ForeignKey("model_routes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="model-route.v2",
        ),
        sa.Column("config_snapshot", JSON_TYPE, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="published"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "source_version_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "model_route_versions.id",
                name="fk_route_versions_source",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("version >= 1", name="ck_model_route_versions_version_positive"),
        sa.CheckConstraint(
            "status in ('draft', 'published', 'disabled', 'retired')",
            name="ck_model_route_versions_status_valid",
        ),
        sa.CheckConstraint(
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'disabled', 'retired'))",
            name="ck_model_route_versions_active_status",
        ),
    )
    op.create_index("ix_model_route_versions_route_id", "model_route_versions", ["route_id"])
    op.create_index(
        "ix_model_route_versions_source_version_id",
        "model_route_versions",
        ["source_version_id"],
    )
    op.create_index(
        "uq_model_route_versions_route_version",
        "model_route_versions",
        ["route_id", "version"],
        unique=True,
    )
    op.create_index(
        "uq_model_route_versions_active",
        "model_route_versions",
        ["route_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active = 1"),
    )


def _prepare_model_route_versions(bind) -> None:
    inspector = sa.inspect(bind)
    if not inspector.has_table("model_route_versions"):
        _create_model_route_versions()
        return

    columns = {column["name"] for column in inspector.get_columns("model_route_versions")}
    checks = {
        constraint.get("name")
        for constraint in inspector.get_check_constraints("model_route_versions")
    }
    indexes = {
        index.get("name"): (
            bool(index.get("unique")),
            tuple(index.get("column_names") or ()),
        )
        for index in inspector.get_indexes("model_route_versions")
    }
    expected_indexes = {
        "ix_model_route_versions_route_id": (False, ("route_id",)),
        "ix_model_route_versions_source_version_id": (False, ("source_version_id",)),
        "uq_model_route_versions_route_version": (True, ("route_id", "version")),
        "uq_model_route_versions_active": (True, ("route_id",)),
    }
    if columns != _ROUTE_VERSION_COLUMNS:
        raise RuntimeError(
            "pre-created model_route_versions columns do not match migration 0064"
        )
    if not _ROUTE_VERSION_CHECKS.issubset(checks):
        raise RuntimeError(
            "pre-created model_route_versions constraints do not match migration 0064"
        )
    if any(indexes.get(name) != expected for name, expected in expected_indexes.items()):
        raise RuntimeError(
            "pre-created model_route_versions indexes do not match migration 0064"
        )


def upgrade() -> None:
    bind = op.get_bind()
    _prepare_model_route_versions(bind)
    metadata = sa.MetaData()
    routes = sa.Table("model_routes", metadata, autoload_with=bind)
    route_versions = sa.Table("model_route_versions", metadata, autoload_with=bind)
    now = datetime.now(timezone.utc)
    for route in bind.execute(sa.select(routes)).mappings():
        existing = bind.scalar(
            sa.select(sa.func.count())
            .select_from(route_versions)
            .where(route_versions.c.route_id == route["id"])
        )
        if existing:
            continue
        bind.execute(
            route_versions.insert().values(
                route_id=route["id"],
                version=max(1, int(route["config_revision"] or 1)),
                config_snapshot=_route_config(route),
                status="published",
                is_active=True,
                activated_at=route["created_at"] or now,
            )
        )

    with op.batch_alter_table("tool_versions") as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(16), nullable=False, server_default="published")
        )
        batch_op.add_column(
            sa.Column(
                "source_version_id",
                sa.BigInteger(),
                sa.ForeignKey(
                    "tool_versions.id",
                    name="fk_tool_versions_source",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch_op.alter_column(
            "activated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
            existing_server_default=sa.func.now(),
            server_default=None,
        )
    op.execute(
        sa.text(
            "UPDATE tool_versions SET status = 'disabled', disabled_at = retired_at, "
            "retired_at = NULL WHERE NOT is_active"
        )
    )
    with op.batch_alter_table("tool_versions") as batch_op:
        batch_op.create_index(
            "ix_tool_versions_source_version_id", ["source_version_id"], unique=False
        )
        batch_op.create_check_constraint(
            "ck_tool_versions_status_valid",
            "status in ('draft', 'published', 'disabled', 'retired')",
        )
        batch_op.create_check_constraint(
            "ck_tool_versions_active_status",
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'published', 'disabled', 'retired'))",
        )

    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.add_column(sa.Column("compiled_snapshot", JSON_TYPE, nullable=True))
        batch_op.add_column(sa.Column("compiled_snapshot_hash", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True))

    metadata = sa.MetaData()
    workflows = sa.Table("workflow_runs", metadata, autoload_with=bind)
    tool_runs = sa.Table("tool_runs", metadata, autoload_with=bind)
    tool_definitions = sa.Table("tool_definitions", metadata, autoload_with=bind)
    tool_versions = sa.Table("tool_versions", metadata, autoload_with=bind)
    quotes = sa.Table("generation_quotes", metadata, autoload_with=bind)
    project_tasks = sa.Table("media_project_tasks", metadata, autoload_with=bind)
    for workflow in bind.execute(sa.select(workflows)).mappings():
        tool_run = bind.execute(
            sa.select(tool_runs).where(tool_runs.c.id == workflow["tool_run_id"])
        ).mappings().first()
        tool_definition = None
        tool_version = None
        quote = None
        if tool_run is not None:
            tool_definition = bind.execute(
                sa.select(tool_definitions).where(
                    tool_definitions.c.id == tool_run["tool_definition_id"]
                )
            ).mappings().first()
            tool_version = bind.execute(
                sa.select(tool_versions).where(
                    tool_versions.c.id == tool_run["tool_version_id"]
                )
            ).mappings().first()
            if tool_run["quote_id"] is not None:
                quote = bind.execute(
                    sa.select(quotes).where(quotes.c.id == tool_run["quote_id"])
                ).mappings().first()
        project_id = bind.scalar(
            sa.select(project_tasks.c.project_id)
            .where(
                project_tasks.c.task_kind == "workflow",
                project_tasks.c.task_id == workflow["id"],
            )
            .order_by(project_tasks.c.id)
            .limit(1)
        )
        snapshot = _legacy_compiled_snapshot(
            workflow["workflow_snapshot"],
            tool_run,
            tool_definition,
            tool_version,
            quote,
            project_id,
        )
        bind.execute(
            workflows.update()
            .where(workflows.c.id == workflow["id"])
            .values(
                compiled_snapshot=snapshot,
                compiled_snapshot_hash=_canonical_hash(snapshot),
                compiled_at=workflow["created_at"] or now,
            )
        )

    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.alter_column(
            "compiled_snapshot", existing_type=JSON_TYPE, nullable=False
        )
        batch_op.alter_column(
            "compiled_snapshot_hash",
            existing_type=sa.String(64),
            nullable=False,
        )
        batch_op.alter_column(
            "compiled_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        )
        batch_op.create_check_constraint(
            "ck_workflow_runs_compiled_snapshot_hash_length",
            "length(compiled_snapshot_hash) = 64",
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.drop_constraint(
            "ck_workflow_runs_compiled_snapshot_hash_length",
            type_="check",
        )
        batch_op.drop_column("compiled_at")
        batch_op.drop_column("compiled_snapshot_hash")
        batch_op.drop_column("compiled_snapshot")

    with op.batch_alter_table("tool_versions") as batch_op:
        batch_op.drop_constraint("ck_tool_versions_active_status", type_="check")
        batch_op.drop_constraint("ck_tool_versions_status_valid", type_="check")
        batch_op.drop_index("ix_tool_versions_source_version_id")
    op.execute(
        sa.text(
            "UPDATE tool_versions SET activated_at = COALESCE(activated_at, created_at, CURRENT_TIMESTAMP), "
            "retired_at = CASE WHEN is_active THEN NULL ELSE "
            "COALESCE(retired_at, disabled_at, updated_at, created_at, CURRENT_TIMESTAMP) END"
        )
    )
    with op.batch_alter_table("tool_versions") as batch_op:
        batch_op.alter_column(
            "activated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            existing_server_default=None,
            server_default=sa.func.now(),
        )
        batch_op.drop_column("updated_at")
        batch_op.drop_column("disabled_at")
        batch_op.drop_column("source_version_id")
        batch_op.drop_column("status")

    op.drop_table("model_route_versions")
