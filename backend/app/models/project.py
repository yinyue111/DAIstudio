"""Auto-generated from models.py split. DO NOT edit the line ranges manually."""
from __future__ import annotations

from datetime import datetime

from ._base import (
    Base,
    BigInteger,
    BigIntPK,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Mapped,
    String,
    func,
    mapped_column,
)


class MediaProject(Base):
    __tablename__ = "media_projects"
    __table_args__ = (
        CheckConstraint(
            "project_type in ('image', 'video', 'mixed')",
            name="ck_media_projects_type_valid",
        ),
        CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_media_projects_status_valid",
        ),
        CheckConstraint(
            "auto_archive_after_days IS NULL OR "
            "(auto_archive_after_days >= 1 AND auto_archive_after_days <= 3650)",
            name="ck_media_projects_auto_archive_days_valid",
        ),
        Index("ix_media_projects_user_status_updated", "user_id", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    project_type: Mapped[str] = mapped_column(String(16), default="mixed", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    cover_asset_ref: Mapped[str | None] = mapped_column(String(512))
    auto_archive_after_days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
class MediaProjectAsset(Base):
    __tablename__ = "media_project_assets"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="ck_media_project_assets_sort_order_nonnegative"),
        Index("uq_media_project_assets_project_asset", "project_id", "asset_ref", unique=True),
        Index("ix_media_project_assets_project_sort", "project_id", "sort_order", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("media_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="source", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class MediaProjectRecipe(Base):
    __tablename__ = "media_project_recipes"
    __table_args__ = (
        Index("uq_media_project_recipes_project_recipe", "project_id", "recipe_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("media_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class MediaProjectTask(Base):
    __tablename__ = "media_project_tasks"
    __table_args__ = (
        CheckConstraint(
            "task_kind in ('generation', 'reverse', 'parse', 'workflow')",
            name="ck_media_project_tasks_kind_valid",
        ),
        Index(
            "uq_media_project_tasks_project_kind_task",
            "project_id",
            "task_kind",
            "task_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("media_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
