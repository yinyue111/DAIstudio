"""add dedicated prompt optimizer model

Revision ID: 0033_prompt_optimizer_model
Revises: 0032_admin_quota_business_window
Create Date: 2026-07-12
"""

from alembic import op

revision = "0033_prompt_optimizer_model"
down_revision = "0032_admin_quota_business_window"
branch_labels = None
depends_on = None


def _replace_use_constraint(values: str) -> None:
    with op.batch_alter_table("model_configs") as batch:
        batch.drop_constraint("ck_model_configs_use_valid", type_="check")
        batch.create_check_constraint("ck_model_configs_use_valid", f"use in ({values})")


def upgrade() -> None:
    _replace_use_constraint("'vision', 'image', 'video', 'prompt'")
    with op.batch_alter_table("gateway_calls") as batch:
        batch.drop_constraint("ck_gateway_calls_kind_valid", type_="check")
        batch.create_check_constraint(
            "ck_gateway_calls_kind_valid",
            "kind in ('reverse', 'prompt_optimize', 'image', 'video_submit', 'video_poll', 'video_download')",
        )
    op.execute(
        """
        INSERT INTO model_configs (
            use, model_id, provider, base_url, api_key_encrypted, gateway_format,
            cost_credits, unlock_cost, enabled, extra
        )
        SELECT
            'prompt', 'gemini-3.5-flash-low', NULL, NULL, NULL, NULL,
            1, 0, TRUE, NULL
        WHERE NOT EXISTS (SELECT 1 FROM model_configs WHERE use = 'prompt')
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM model_configs WHERE use = 'prompt'")
    with op.batch_alter_table("gateway_calls") as batch:
        batch.drop_constraint("ck_gateway_calls_kind_valid", type_="check")
        batch.create_check_constraint(
            "ck_gateway_calls_kind_valid",
            "kind in ('reverse', 'image', 'video_submit', 'video_poll', 'video_download')",
        )
    _replace_use_constraint("'vision', 'image', 'video'")
