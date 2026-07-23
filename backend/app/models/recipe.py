"""Auto-generated from models.py split. DO NOT edit the line ranges manually."""
from __future__ import annotations

from datetime import datetime

from ._base import (
    Base,
    BigInteger,
    BigIntPK,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSONType,
    Mapped,
    String,
    Text,
    func,
    mapped_column,
    text,
)


class CreationRecipe(Base):
    __tablename__ = "creation_recipes"
    __table_args__ = (
        CheckConstraint("category in ('image', 'video')", name="ck_creation_recipes_category_valid"),
        CheckConstraint(
            "visibility in ('private', 'public')", name="ck_creation_recipes_visibility_valid"
        ),
        CheckConstraint(
            "moderation_status in ('draft', 'pending', 'approved', 'rejected')",
            name="ck_creation_recipes_moderation_status_valid",
        ),
        CheckConstraint("current_version >= 1", name="ck_creation_recipes_version_positive"),
        CheckConstraint(
            "approved_version IS NULL OR approved_version >= 1",
            name="ck_creation_recipes_approved_version_positive",
        ),
        Index("ix_creation_recipes_user_updated", "user_id", "updated_at"),
        Index("ix_creation_recipes_public_updated", "visibility", "updated_at"),
        Index(
            "ix_creation_recipes_moderation_updated",
            "moderation_status",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_operation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), default="private", nullable=False)
    moderation_status: Mapped[str] = mapped_column(
        String(16), default="draft", server_default="draft", nullable=False
    )
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    approved_version: Mapped[int | None] = mapped_column(Integer)
    cover_asset_url: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    review_note: Mapped[str | None] = mapped_column(String(500))
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
class CreationRecipeVersion(Base):
    __tablename__ = "creation_recipe_versions"
    __table_args__ = (
        Index(
            "uq_creation_recipe_versions_recipe_version", "recipe_id", "version", unique=True
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="creation-recipe.v1", nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    metadata_snapshot: Mapped[dict] = mapped_column(
        JSONType,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class CreationRecipeShare(Base):
    __tablename__ = "creation_recipe_shares"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_creation_recipe_shares_version_positive"),
        CheckConstraint(
            "status in ('active', 'revoked')",
            name="ck_creation_recipe_shares_status_valid",
        ),
        Index("uq_creation_recipe_shares_slug", "slug", unique=True),
        Index(
            "ix_creation_recipe_shares_recipe_status_created",
            "recipe_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_creation_recipe_shares_owner_created",
            "owner_user_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class CreationRecipeUsageEvent(Base):
    __tablename__ = "creation_recipe_usage_events"
    __table_args__ = (
        CheckConstraint(
            "event_type in ('apply', 'clone', 'generation_prepare', 'generation_submit')",
            name="ck_creation_recipe_usage_events_type_valid",
        ),
        CheckConstraint(
            "source in ('owner', 'public', 'share')",
            name="ck_creation_recipe_usage_events_source_valid",
        ),
        CheckConstraint(
            "recipe_version >= 1",
            name="ck_creation_recipe_usage_events_version_positive",
        ),
        Index(
            "uq_creation_recipe_usage_events_user_client",
            "user_id",
            "client_event_id",
            unique=True,
        ),
        Index(
            "ix_creation_recipe_usage_events_recipe_created",
            "recipe_id",
            "created_at",
        ),
        Index(
            "ix_creation_recipe_usage_events_generation_task",
            "generation_task_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    recipe_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="SET NULL")
    )
    recipe_version: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    share_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("creation_recipe_shares.id", ondelete="SET NULL"), index=True
    )
    derived_recipe_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="SET NULL"), index=True
    )
    generation_task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("gen_tasks.id", ondelete="SET NULL")
    )
    client_event_id: Mapped[str | None] = mapped_column(String(128))
    context: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
