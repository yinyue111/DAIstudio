"""add versioned tool catalog

Revision ID: 0045_tool_catalog
Revises: 0044_model_versions_and_quotes
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045_tool_catalog"
down_revision = "0044_model_versions_and_quotes"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


_DEFAULT_TOOLS = (
    {
        "slug": "image-reverse",
        "name": "图片反推",
        "description": "从图片提取可审阅的生成稿、结构参数和区域证据。",
        "category": "image",
        "renderer": "studio",
        "entry_path": "/?workflow=image-reverse",
        "icon": "scan-search",
        "sort_order": 10,
        "featured": True,
        "workflow": {
            "type": "studio_preset",
            "creation_mode": "image_edit",
            "reverse": True,
            "analysis_focus": "comprehensive",
        },
        "capabilities": {"reverse": True, "evidence": True, "batch": True},
    },
    {
        "slug": "video-reverse",
        "name": "视频反推",
        "description": "按片段、关键帧和音频证据拆解视频并生成可执行分镜。",
        "category": "video",
        "renderer": "studio",
        "entry_path": "/?workflow=video-reverse",
        "icon": "film",
        "sort_order": 20,
        "featured": True,
        "workflow": {
            "type": "studio_preset",
            "creation_mode": "video_edit",
            "reverse": True,
            "analysis_focus": "storyboard",
        },
        "capabilities": {
            "reverse": True,
            "timeline_evidence": True,
            "audio_analysis": True,
            "batch": True,
        },
    },
    {
        "slug": "image-replica",
        "name": "一键仿图",
        "description": "参考图分析、字段审阅、模型适配和图片生成的一体化配方。",
        "category": "workflow",
        "renderer": "studio",
        "entry_path": "/?workflow=image-replica",
        "icon": "copy-image",
        "sort_order": 30,
        "featured": True,
        "workflow": {
            "type": "studio_preset",
            "creation_mode": "image_edit",
            "reverse": True,
            "analysis_focus": "replica",
            "output_purpose": "generation",
        },
        "capabilities": {"reverse": True, "generate": True, "recipe": True},
    },
    {
        "slug": "first-last-frame",
        "name": "首尾帧视频",
        "description": "使用首帧和尾帧约束生成可控转场视频。",
        "category": "workflow",
        "renderer": "studio",
        "entry_path": "/?workflow=first-last-frame",
        "icon": "between-horizontal-start",
        "sort_order": 40,
        "featured": False,
        "workflow": {
            "type": "studio_preset",
            "creation_mode": "video_edit",
            "reference_roles": ["first_frame", "last_frame"],
        },
        "capabilities": {"generate": True, "first_last_frame": True},
    },
)


def upgrade() -> None:
    tables = _tables()
    if "tool_definitions" not in tables:
        op.create_table(
            "tool_definitions",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("description", sa.String(512), nullable=True),
            sa.Column("category", sa.String(16), nullable=False),
            sa.Column("renderer", sa.String(64), nullable=False, server_default="studio"),
            sa.Column("entry_path", sa.String(512), nullable=False),
            sa.Column("icon", sa.String(64), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "category in ('image', 'video', 'workflow', 'utility')",
                name="ck_tool_definitions_category_valid",
            ),
        )
        op.create_index("uq_tool_definitions_slug", "tool_definitions", ["slug"], unique=True)
        op.create_index(
            "ix_tool_definitions_enabled_sort",
            "tool_definitions",
            ["enabled", "sort_order", "id"],
        )

    if "tool_versions" not in tables:
        op.create_table(
            "tool_versions",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "tool_definition_id",
                sa.BigInteger(),
                sa.ForeignKey("tool_definitions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("schema_version", sa.String(32), nullable=False, server_default="tool.v1"),
            sa.Column("input_schema", JSON_TYPE, nullable=False),
            sa.Column("workflow", JSON_TYPE, nullable=False),
            sa.Column("pricing_policy", JSON_TYPE, nullable=False),
            sa.Column("capabilities", JSON_TYPE, nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("version >= 1", name="ck_tool_versions_version_positive"),
        )
        op.create_index(
            "ix_tool_versions_tool_definition_id",
            "tool_versions",
            ["tool_definition_id"],
        )
        op.create_index(
            "uq_tool_versions_definition_version",
            "tool_versions",
            ["tool_definition_id", "version"],
            unique=True,
        )
        op.create_index(
            "uq_tool_versions_active",
            "tool_versions",
            ["tool_definition_id"],
            unique=True,
            postgresql_where=sa.text("is_active"),
            sqlite_where=sa.text("is_active = 1"),
        )

    bind = op.get_bind()
    metadata = sa.MetaData()
    definitions = sa.Table("tool_definitions", metadata, autoload_with=bind)
    versions = sa.Table("tool_versions", metadata, autoload_with=bind)
    for seed in _DEFAULT_TOOLS:
        tool_id = bind.scalar(
            sa.select(definitions.c.id).where(definitions.c.slug == seed["slug"])
        )
        if tool_id is None:
            values = {
                key: seed[key]
                for key in (
                    "slug",
                    "name",
                    "description",
                    "category",
                    "renderer",
                    "entry_path",
                    "icon",
                    "sort_order",
                    "featured",
                )
            }
            bind.execute(definitions.insert().values(**values, enabled=True))
            tool_id = bind.scalar(
                sa.select(definitions.c.id).where(definitions.c.slug == seed["slug"])
            )
        version_exists = bind.scalar(
            sa.select(versions.c.id).where(versions.c.tool_definition_id == tool_id)
        )
        if version_exists is None:
            bind.execute(
                versions.insert().values(
                    tool_definition_id=tool_id,
                    version=1,
                    schema_version="tool.v1",
                    input_schema={},
                    workflow=seed["workflow"],
                    pricing_policy={"type": "server_quote"},
                    capabilities=seed["capabilities"],
                    is_active=True,
                )
            )


def downgrade() -> None:
    tables = _tables()
    if "tool_versions" in tables:
        op.drop_table("tool_versions")
    if "tool_definitions" in tables:
        op.drop_table("tool_definitions")
