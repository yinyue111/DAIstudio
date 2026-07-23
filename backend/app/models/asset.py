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
    func,
    mapped_column,
)


class AssetFolder(Base):
    __tablename__ = "asset_folders"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="ck_asset_folders_sort_order_nonnegative"),
        Index("ix_asset_folders_user_parent_sort", "user_id", "parent_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("asset_folders.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
class AssetFolderItem(Base):
    __tablename__ = "asset_folder_items"
    __table_args__ = (
        Index("uq_asset_folder_items_user_asset", "user_id", "asset_ref", unique=True),
        Index("ix_asset_folder_items_folder_created", "folder_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    folder_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("asset_folders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class UserAssetMetadata(Base):
    """User-owned metadata shared by generated and uploaded asset refs."""

    __tablename__ = "user_asset_metadata"
    __table_args__ = (
        CheckConstraint(
            "media_type in ('image', 'video')",
            name="ck_user_asset_metadata_media_type_valid",
        ),
        CheckConstraint(
            "analysis_status in ('pending', 'ready', 'degraded')",
            name="ck_user_asset_metadata_analysis_status_valid",
        ),
        Index(
            "uq_user_asset_metadata_user_asset",
            "user_id",
            "asset_ref",
            unique=True,
        ),
        Index(
            "ix_user_asset_metadata_user_content_hash",
            "user_id",
            "content_sha256",
        ),
        Index(
            "ix_user_asset_metadata_user_perceptual_hash",
            "user_id",
            "perceptual_hash",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(8), nullable=False)
    tags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    perceptual_hash: Mapped[str | None] = mapped_column(String(16))
    perceptual_hash_algorithm: Mapped[str | None] = mapped_column(String(32))
    analysis_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    analysis_error: Mapped[str | None] = mapped_column(String(500))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
class UploadedAsset(Base):
    __tablename__ = "uploaded_assets"
    __table_args__ = (
        CheckConstraint("bytes IS NULL OR bytes >= 0", name="ck_uploaded_assets_bytes_nonnegative"),
        CheckConstraint("duration IS NULL OR duration >= 0", name="ck_uploaded_assets_duration_nonnegative"),
        CheckConstraint("origin in ('uploaded', 'fetched')", name="ck_uploaded_assets_origin_valid"),
        Index("ix_uploaded_assets_user_created", "user_id", "created_at"),
        Index(
            "ix_uploaded_assets_user_favorite_created",
            "user_id",
            "favorite",
            "created_at",
        ),
        Index("ix_uploaded_assets_user_retained", "user_id", "retained_at"),
    )

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    mime: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    origin: Mapped[str] = mapped_column(String(16), default="uploaded", server_default="uploaded", nullable=False)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class AssetReport(Base):
    __tablename__ = "asset_reports"
    __table_args__ = (
        CheckConstraint(
            "status in ('open', 'dismissed', 'takedown')",
            name="ck_asset_reports_status_valid",
        ),
        CheckConstraint(
            "reason in ('copyright', 'sensitive', 'illegal', 'privacy', 'other')",
            name="ck_asset_reports_reason_valid",
        ),
        Index("ix_asset_reports_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    asset_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("gen_assets.id"), index=True)
    reporter_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    owner_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    handled_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    handle_note: Mapped[str | None] = mapped_column(String(500))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
