"""fix Grok video reference request schema

Revision ID: 0076_grok_video_ref_schema
Revises: 0075_grok_video_product_refs
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0076_grok_video_ref_schema"
down_revision = "0075_grok_video_product_refs"
branch_labels = None
depends_on = None

_MODEL_ID = "grok-imagine-video-1.5"
_OLD_MAPPING_KEYS = (
    "product_images_field",
    "product_images_item_field",
    "product_image_field",
    "product_image_item_field",
    "product_detail_images_field",
    "product_detail_images_item_field",
)
_NATIVE_MAPPING = {
    "first_frame_field": "image",
    "first_frame_item_field": "url",
    "product_images_field": "reference_images",
    "product_images_item_field": "url",
    "negative_prompt_mode": "append_to_prompt",
}


def _matching_rows(bind, model_configs):
    return (
        bind.execute(
            sa.select(model_configs).where(
                model_configs.c.use == "video",
                model_configs.c.model_id == _MODEL_ID,
                model_configs.c.provider == "grok",
                model_configs.c.gateway_format == "openai",
            )
        )
        .mappings()
        .all()
    )


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=bind)
    for row in _matching_rows(bind, model_configs):
        extra = dict(row["extra"] or {}) if isinstance(row.get("extra"), dict) else {}
        for key in _OLD_MAPPING_KEYS:
            extra.pop(key, None)
        extra.update(_NATIVE_MAPPING)
        bind.execute(
            sa.update(model_configs)
            .where(model_configs.c.id == int(row["id"]))
            .values(extra=extra)
        )


def downgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=bind)
    for row in _matching_rows(bind, model_configs):
        extra = dict(row["extra"] or {}) if isinstance(row.get("extra"), dict) else {}
        for key in _NATIVE_MAPPING:
            extra.pop(key, None)
        extra.update(
            {
                "product_images_field": "images",
                "product_images_item_field": "url",
            }
        )
        bind.execute(
            sa.update(model_configs)
            .where(model_configs.c.id == int(row["id"]))
            .values(extra=extra)
        )
