"""publish the executable storyboard composition tool

Revision ID: 0057_storyboard_compose_tool
Revises: 0056_workflow_dispatch_outbox
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0057_storyboard_compose_tool"
down_revision = "0056_workflow_dispatch_outbox"
branch_labels = None
depends_on = None


TOOL_SLUG = "storyboard-compose"

INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["composition"],
    "properties": {
        "composition": {
            "type": "object",
            "additionalProperties": False,
            "required": ["shots"],
            "properties": {
                "schema_version": {"const": "video-composition.v1"},
                "title": {"type": "string", "minLength": 1, "maxLength": 128},
                "reverse_operation_id": {"type": ["integer", "null"], "minimum": 1},
                "canvas": {"type": "object"},
                "shots": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "required": ["shot_id", "asset_ref"],
                    },
                },
                "subtitles": {"type": "array", "maxItems": 200},
                "audio_tracks": {"type": "array", "maxItems": 8},
                "original_audio_volume": {"type": "number", "minimum": 0, "maximum": 2},
            },
        }
    },
}

WORKFLOW = {
    "type": "workflow.v1",
    "studio_preset": {
        "creation_mode": "video_edit",
        "analysis_focus": "storyboard",
        "output_purpose": "storyboard",
        "message": "已进入分镜合成，请先恢复视频反推分镜并为镜头绑定素材。",
    },
    "nodes": [
        {
            "key": "compose",
            "type": "compose",
            "depends_on": [],
            "config": {"timeout_seconds": 2400},
            "max_attempts": 2,
            "side_effect": True,
            "compensation": {"type": "cleanup_video_composition", "config": {}},
        },
        {
            "key": "export",
            "type": "export",
            "depends_on": ["compose"],
            "config": {},
            "max_attempts": 2,
            "side_effect": False,
        },
    ],
    "output_node": "export",
}


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    definitions = sa.Table("tool_definitions", metadata, autoload_with=bind)
    versions = sa.Table("tool_versions", metadata, autoload_with=bind)

    tool_id = bind.scalar(sa.select(definitions.c.id).where(definitions.c.slug == TOOL_SLUG))
    if tool_id is None:
        bind.execute(
            definitions.insert().values(
                slug=TOOL_SLUG,
                name="分镜合成与导出",
                description="将逐镜视频、字幕和音轨合成为可下载 MP4。",
                category="workflow",
                renderer="studio",
                entry_path="/?workflow=storyboard-compose",
                icon="clapperboard",
                sort_order=50,
                enabled=True,
                featured=True,
            )
        )
        tool_id = bind.scalar(
            sa.select(definitions.c.id).where(definitions.c.slug == TOOL_SLUG)
        )

    existing = bind.scalar(
        sa.select(versions.c.id).where(
            versions.c.tool_definition_id == tool_id,
            versions.c.version == 1,
        )
    )
    if existing is None:
        bind.execute(
            versions.insert().values(
                tool_definition_id=tool_id,
                version=1,
                schema_version="tool.v1",
                input_schema=INPUT_SCHEMA,
                workflow=WORKFLOW,
                pricing_policy={"type": "server_quote", "credits": 0},
                capabilities={
                    "compose": True,
                    "export": True,
                    "subtitles": True,
                    "audio_mix": True,
                    "transitions": True,
                },
                is_active=True,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    definitions = sa.Table("tool_definitions", metadata, autoload_with=bind)
    versions = sa.Table("tool_versions", metadata, autoload_with=bind)
    tool_runs = sa.Table("tool_runs", metadata, autoload_with=bind)

    tool_id = bind.scalar(sa.select(definitions.c.id).where(definitions.c.slug == TOOL_SLUG))
    if tool_id is None:
        return
    has_runs = bind.scalar(
        sa.select(sa.func.count()).select_from(tool_runs).where(
            tool_runs.c.tool_definition_id == tool_id
        )
    )
    if has_runs:
        bind.execute(
            definitions.update().where(definitions.c.id == tool_id).values(enabled=False)
        )
        return
    bind.execute(versions.delete().where(versions.c.tool_definition_id == tool_id))
    bind.execute(definitions.delete().where(definitions.c.id == tool_id))
