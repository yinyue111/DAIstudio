"""allow anthropic-compatible model gateways

Revision ID: 0034_anthropic_gateway_format
Revises: 0033_prompt_optimizer_model
Create Date: 2026-07-12
"""

from alembic import op

revision = "0034_anthropic_gateway_format"
down_revision = "0033_prompt_optimizer_model"
branch_labels = None
depends_on = None


def _replace_constraint(values: str) -> None:
    with op.batch_alter_table("model_configs") as batch:
        batch.drop_constraint("ck_model_configs_gateway_format_valid", type_="check")
        batch.create_check_constraint(
            "ck_model_configs_gateway_format_valid",
            f"gateway_format IS NULL OR gateway_format in ({values})",
        )


def upgrade() -> None:
    _replace_constraint("'openai', 'ark', 'anthropic'")


def downgrade() -> None:
    op.execute("UPDATE model_configs SET gateway_format = 'openai' WHERE gateway_format = 'anthropic'")
    _replace_constraint("'openai', 'ark'")
