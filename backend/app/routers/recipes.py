"""Versioned creation recipes restored into the shared Studio workflow."""
from __future__ import annotations

import re
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import (
    CreationRecipe,
    CreationRecipeShare,
    CreationRecipeUsageEvent,
    CreationRecipeVersion,
    GenTask,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
    ReverseOperation,
    User,
)
from ..schemas import (
    CreationRecipeIn,
    CreationRecipeOut,
    CreationRecipeSharedOut,
    CreationRecipeShareIn,
    CreationRecipeShareOut,
    CreationRecipeUsageEventOut,
    CreationRecipeUsageIn,
    CreationRecipeUsageSummaryOut,
    CreationRecipeVersionIn,
    CreationRecipeVersionOut,
    validate_creation_recipe_payload,
)
from ..services import audit, recipe_usage
from ..services.content_safety import assert_text_allowed

router = APIRouter(prefix="/api/recipes", tags=["recipes"])

_PUBLIC_ASSET_ACCESS_KEY = "public_asset_access"
_PUBLIC_IDENTIFIER_KEYS = {
    "derived_from_recipe_id",
    "model_config_id",
    "target_model_config_id",
}
_SECRET_KEY_SEGMENTS = {
    "authorization",
    "cookie",
    "credential",
    "password",
    "sas",
    "secret",
    "signature",
    "token",
}
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "auth_key",
    "private_key",
}
_URI_SCHEME_RE = re.compile(
    r"(?i)(?:https?|ftps?|data|blob|file|s3|gs|ipfs|azure|oss|urn|mailto|tel|cid|magnet):[^\s\"'<>]+"
)
_DOMAIN_PATH_RE = re.compile(
    r"(?i)(?<![a-z0-9._-])(?:www\.)?"
    r"[a-z0-9](?:[a-z0-9-]{0,62}\.)+[a-z]{2,63}"
    r"(?::\d{1,5})?/[^\s\"'<>]*"
)
_BARE_HOST_PATH_RE = re.compile(
    r"(?i)(?<![a-z0-9._-])"
    r"(?:localhost|\[[0-9a-f:]+\]|(?:\d{1,3}\.){3}\d{1,3})"
    r"(?::\d{1,5})?/[^\s\"'<>]*"
)


def _recipe_audit_snapshot(row: CreationRecipe) -> dict[str, Any]:
    return {
        "owner_user_id": int(row.user_id),
        "category": row.category,
        "visibility": row.visibility,
        "moderation_status": row.moderation_status,
        "favorite": bool(row.favorite),
        "current_version": int(row.current_version),
        "approved_version": int(row.approved_version) if row.approved_version else None,
        "source_operation_id": (
            int(row.source_operation_id) if row.source_operation_id is not None else None
        ),
        "deleted": row.deleted_at is not None,
    }


def _audit_recipe_change(
    db: Session,
    request: Request,
    user: User,
    action: str,
    row: CreationRecipe,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    audit.log_required(
        db,
        user_id=int(user.id),
        action=action,
        biz_type="creation_recipe",
        biz_id=int(row.id),
        ip=get_client_ip(request),
        detail={**_recipe_audit_snapshot(row), **(detail or {})},
    )


def _recipe_version_metadata(
    row: CreationRecipe,
    *,
    origin: str = "recorded",
) -> dict[str, Any]:
    return {
        "schema_version": "creation-recipe-metadata.v1",
        "origin": origin,
        "title": row.title,
        "visibility": row.visibility,
        "cover_asset_url": row.cover_asset_url,
    }


def _apply_recipe_version_metadata(
    row: CreationRecipe,
    version: CreationRecipeVersion,
) -> None:
    snapshot = version.metadata_snapshot if isinstance(version.metadata_snapshot, dict) else {}
    title = snapshot.get("title")
    visibility = snapshot.get("visibility")
    if isinstance(title, str) and title.strip():
        row.title = title.strip()
    if visibility in {"private", "public"}:
        row.visibility = visibility
    if "cover_asset_url" in snapshot:
        row.cover_asset_url = snapshot.get("cover_asset_url")


_PROTECTED_PATH_RE = re.compile(
    r"(?i)(?<![a-z0-9_])"
    r"(?:media|api/(?:uploads|assets))(?:/|[?#])"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![a-z0-9._-])[a-z]:/(?!/)"
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![a-z0-9._-])/(?!/)"
    r"(?:[^/\s\"'<>]+/)+[^/\s\"'<>]*"
)
_POSIX_ABSOLUTE_FILE_RE = re.compile(
    r"(?i)(?<![a-z0-9._-])/(?!/)"
    r"[^/\s\"'<>]+\.[a-z0-9]{1,16}(?=$|[?#\s\"'<>,;:!?)\]}])"
)
_DOT_RELATIVE_PATH_RE = re.compile(
    r"(?<![a-z0-9_])(?:\.\.?/)+"
    r"[^/\s\"'<>]+(?:/[^/\s\"'<>]+)*"
)
_SINGLE_SEGMENT_ABSOLUTE_PATH_RE = re.compile(r"/[^/\s\"'<>]+")
_PUBLIC_SLASH_COMMANDS = frozenset({"/imagine"})
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:\bbearer\s+[a-z0-9._~+/=-]+|"
    r"(?:^|[?&;\s])(?:access_token|api_key|credential|sig|signature|token|x-amz-signature)=[^\s&;]+)"
)
_REDACTED = object()
_MAX_URL_DECODE_ROUNDS = 4
_MAX_PUBLIC_PAYLOAD_DEPTH = 64
_MAX_PUBLIC_PAYLOAD_NODES = 50_000
_MAX_PUBLIC_COLLECTION_ITEMS = 10_000
_MAX_PUBLIC_STRING_LENGTH = 1_000_000


class _PublicSanitizeBudget:
    __slots__ = ("remaining_nodes",)

    def __init__(self) -> None:
        self.remaining_nodes = _MAX_PUBLIC_PAYLOAD_NODES

    def consume_node(self) -> bool:
        if self.remaining_nodes <= 0:
            return False
        self.remaining_nodes -= 1
        return True


class CreationRecipeMetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=128)
    visibility: Literal["private", "public"] | None = None
    cover_asset_url: str | None = Field(default=None, max_length=4096)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("配方标题不能为空")
        return normalized

    @field_validator("cover_asset_url")
    @classmethod
    def _cover(cls, value: str | None) -> str | None:
        return _normalize_cover_url(value)

    @model_validator(mode="after")
    def _has_update(self):
        if not self.model_fields_set:
            raise ValueError("请至少提交一个可修改字段")
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("配方标题不能为空")
        if "visibility" in self.model_fields_set and self.visibility is None:
            raise ValueError("配方可见性不能为空")
        return self


class CreationRecipeCloneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(default=None, gt=0)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    share_slug: str | None = Field(
        default=None,
        min_length=20,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("配方标题不能为空")
        return normalized

    @field_validator("share_slug")
    @classmethod
    def _share_slug(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


def _normalize_cover_url(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if "\\" in normalized or normalized.startswith("//"):
        raise ValueError("配方封面地址不合法")
    parsed = urlsplit(normalized)
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("配方封面地址仅支持 HTTP(S)")
        if parsed.username or parsed.password:
            raise ValueError("配方封面地址不得包含访问凭据")
        return normalized
    if not normalized.startswith(("/media/", "/api/uploads/")):
        raise ValueError("配方封面相对地址不在允许范围内")
    return normalized


def _positive_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _catalog_version_for_model(
    db: Session,
    *,
    model_config_id: int,
    expected_use: str,
    requested: dict[str, Any],
) -> dict[str, Any] | None:
    model = db.get(ModelConfig, model_config_id)
    if model is None or model.use != expected_use:
        return None

    requested_capability_id = _positive_id(requested.get("capability_version_id"))
    capability = (
        db.get(ModelCapabilityVersion, requested_capability_id)
        if requested_capability_id is not None
        else None
    )
    if capability is None or int(capability.model_config_id) != int(model.id):
        capability = db.execute(
            select(ModelCapabilityVersion).where(
                ModelCapabilityVersion.model_config_id == model.id,
                ModelCapabilityVersion.is_active.is_(True),
            )
        ).scalar_one_or_none()

    requested_price_id = _positive_id(requested.get("price_version_id"))
    price = (
        db.get(ModelPriceVersion, requested_price_id)
        if requested_price_id is not None
        else None
    )
    if price is None or int(price.model_config_id) != int(model.id):
        price = db.execute(
            select(ModelPriceVersion).where(
                ModelPriceVersion.model_config_id == model.id,
                ModelPriceVersion.is_active.is_(True),
            )
        ).scalar_one_or_none()

    return {
        "model_config_id": int(model.id),
        "use": model.use,
        "model_id": model.model_id,
        "capability_version_id": int(capability.id) if capability is not None else None,
        "capability_version": int(capability.version) if capability is not None else None,
        "capability_schema_version": capability.schema_version if capability is not None else None,
        "price_version_id": int(price.id) if price is not None else None,
        "price_version": int(price.version) if price is not None else None,
        "price_schema_version": price.schema_version if price is not None else None,
    }


def _with_catalog_version_snapshot(
    db: Session,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Freeze trusted catalog identities without making Recipe prices authoritative."""
    snapshot = deepcopy(payload)
    generation = snapshot.get("generation")
    if not isinstance(generation, dict):
        return snapshot

    selections = generation.get("model_selections")
    selections = selections if isinstance(selections, dict) else {}
    requested_versions = generation.get("catalog_versions")
    requested_versions = requested_versions if isinstance(requested_versions, dict) else {}
    explicit_ids = {
        "image": generation.get("generation_model_config_id"),
        "video": generation.get("generation_model_config_id"),
        "vision": generation.get("vision_model_config_id"),
        "prompt": generation.get("prompt_model_config_id"),
    }
    catalog_versions: dict[str, dict[str, Any]] = {}
    for use in ("image", "video", "vision", "prompt"):
        model_config_id = _positive_id(selections.get(use) or explicit_ids.get(use))
        if model_config_id is None:
            continue
        requested = requested_versions.get(use)
        requested = requested if isinstance(requested, dict) else {}
        version = _catalog_version_for_model(
            db,
            model_config_id=model_config_id,
            expected_use=use,
            requested=requested,
        )
        if version is not None:
            catalog_versions[use] = version

    generation = deepcopy(generation)
    if catalog_versions:
        generation["catalog_versions"] = catalog_versions
    else:
        generation.pop("catalog_versions", None)
    snapshot["generation"] = generation
    return snapshot


def _normalized_payload_key(value: object) -> str:
    normalized = str(value or "").strip().replace("-", "_").replace(" ", "_")
    if normalized in {"ID", "Id", "id"}:
        return "id"
    if normalized in {"IDs", "Ids", "ids"}:
        return "ids"
    normalized = re.sub(r"(?<=[A-Za-z0-9])(?:IDs|Ids)$", "_ids", normalized)
    normalized = re.sub(r"(?<=[A-Za-z0-9])(?:ID|Id)$", "_id", normalized)
    normalized = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", normalized)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    normalized = normalized.lower().replace("_i_ds", "_ids").replace("_i_d", "_id")
    normalized = normalized.replace("finger_print", "fingerprint")
    return re.sub(r"_+", "_", normalized).strip("_")


def _has_redacted_value(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _redaction_weight(value: Any) -> int:
    if not _has_redacted_value(value):
        return 0
    if isinstance(value, list):
        return max(1, len(value))
    return 1


def _is_internal_identifier_key(key: str) -> bool:
    if key in _PUBLIC_IDENTIFIER_KEYS:
        return False
    parts = set(key.split("_"))
    if {"id", "ids", "uuid", "guid", "ref", "refs", "fingerprint"} & parts:
        return True
    return False


def _is_secret_key(key: str) -> bool:
    return key in _SECRET_KEYS or bool(_SECRET_KEY_SEGMENTS & set(key.split("_")))


def _contains_uri_with_authority(value: str) -> bool:
    """Find authority separators without retrying an unbounded scheme at every byte."""
    cursor = 0
    while True:
        separator = value.find("//", cursor)
        if separator < 0:
            return False
        first_locator_character = separator + 2
        if first_locator_character < len(value):
            character = value[first_locator_character]
            if not character.isspace() and character not in "\"'<>":
                return True
        cursor = first_locator_character


def _contains_filesystem_locator(value: str) -> bool:
    if _DOT_RELATIVE_PATH_RE.search(value):
        return True
    if not _SINGLE_SEGMENT_ABSOLUTE_PATH_RE.fullmatch(value):
        return False
    return value.casefold() not in _PUBLIC_SLASH_COMMANDS


def _contains_url_uri_or_secret(value: str) -> bool:
    current = value.strip().replace("\\", "/")
    if not current:
        return False
    for decode_round in range(_MAX_URL_DECODE_ROUNDS + 1):
        if (
            _contains_uri_with_authority(current)
            or _URI_SCHEME_RE.search(current)
            or _DOMAIN_PATH_RE.search(current)
            or _BARE_HOST_PATH_RE.search(current)
            or _PROTECTED_PATH_RE.search(current)
            or _WINDOWS_ABSOLUTE_PATH_RE.search(current)
            or _POSIX_ABSOLUTE_PATH_RE.search(current)
            or _POSIX_ABSOLUTE_FILE_RE.search(current)
            or _contains_filesystem_locator(current)
            or _SECRET_VALUE_RE.search(current)
        ):
            return True
        if decode_round == _MAX_URL_DECODE_ROUNDS or "%" not in current:
            break
        decoded = unquote(current, errors="replace").replace("\\", "/")
        if decoded == current:
            break
        current = decoded
    # Deeply nested escapes are ambiguous after the bounded decode budget.
    # Public projections fail closed instead of persisting a latent locator.
    return bool(_PERCENT_ESCAPE_RE.search(current))


def _sanitize_public_node(
    node: Any,
    *,
    budget: _PublicSanitizeBudget | None = None,
    depth: int = 0,
) -> tuple[Any, int]:
    if budget is None:
        budget = _PublicSanitizeBudget()
    if depth > _MAX_PUBLIC_PAYLOAD_DEPTH or not budget.consume_node():
        return _REDACTED, max(1, _redaction_weight(node))
    if isinstance(node, dict):
        if len(node) > _MAX_PUBLIC_COLLECTION_ITEMS:
            return _REDACTED, max(1, len(node))
        sanitized: dict[str, Any] = {}
        removed = 0
        for raw_key, child in node.items():
            raw_key_text = str(raw_key)
            if (
                len(raw_key_text) > _MAX_PUBLIC_STRING_LENGTH
                or _contains_url_uri_or_secret(raw_key_text)
            ):
                removed += max(1, _redaction_weight(child))
                continue
            key = _normalized_payload_key(raw_key)
            if key == _PUBLIC_ASSET_ACCESS_KEY:
                continue
            if _is_internal_identifier_key(key) or _is_secret_key(key):
                removed += _redaction_weight(child)
                continue
            clean_child, child_removed = _sanitize_public_node(
                child,
                budget=budget,
                depth=depth + 1,
            )
            removed += child_removed
            if clean_child is not _REDACTED:
                sanitized[raw_key_text] = clean_child
        return sanitized, removed
    if isinstance(node, list):
        if len(node) > _MAX_PUBLIC_COLLECTION_ITEMS:
            return _REDACTED, max(1, len(node))
        sanitized_list: list[Any] = []
        removed = 0
        for child in node:
            clean_child, child_removed = _sanitize_public_node(
                child,
                budget=budget,
                depth=depth + 1,
            )
            removed += child_removed
            if clean_child is not _REDACTED:
                sanitized_list.append(clean_child)
        return sanitized_list, removed
    if isinstance(node, str):
        if (
            len(node) > _MAX_PUBLIC_STRING_LENGTH
            or _contains_url_uri_or_secret(node)
        ):
            return _REDACTED, 1
    return node, 0


def _public_recipe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    previous = payload.get(_PUBLIC_ASSET_ACCESS_KEY)
    sanitized, removed = _sanitize_public_node(payload)
    if sanitized is _REDACTED:
        sanitized = {}
    assert isinstance(sanitized, dict)
    previous_removed = 0
    if isinstance(previous, dict) and previous.get("status") == "unavailable":
        try:
            previous_removed = max(0, int(previous.get("removed_count") or 0))
        except (TypeError, ValueError):
            previous_removed = 0
    removed = max(removed, previous_removed)
    if removed:
        sanitized[_PUBLIC_ASSET_ACCESS_KEY] = {
            "status": "unavailable",
            "reason": "private_source_assets_not_shared",
            "removed_count": removed,
        }
    return sanitized


def _public_recipe_title(value: str) -> str:
    title = str(value or "").strip()
    if not title or _contains_url_uri_or_secret(title):
        return "公开创作配方"
    return title


def _owned_recipe(
    db: Session,
    recipe_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> CreationRecipe:
    query = select(CreationRecipe).where(
        CreationRecipe.id == recipe_id,
        CreationRecipe.user_id == user_id,
        CreationRecipe.deleted_at.is_(None),
    )
    if for_update:
        query = query.with_for_update()
    row = db.execute(query).scalar_one_or_none()
    if row is None or int(row.user_id) != int(user_id):
        raise HTTPException(404, "创作配方不存在")
    return row


def _current_version(db: Session, row: CreationRecipe) -> CreationRecipeVersion | None:
    return db.execute(
        select(CreationRecipeVersion).where(
            CreationRecipeVersion.recipe_id == row.id,
            CreationRecipeVersion.version == row.current_version,
        )
    ).scalar_one_or_none()


def _moderation_is_current(row: CreationRecipe) -> bool:
    return recipe_usage.moderation_is_current(row)


def _revoke_active_shares(
    db: Session,
    recipe_id: int,
    *,
    now: datetime | None = None,
) -> int:
    revoked_at = now or datetime.now(timezone.utc)
    result = db.execute(
        update(CreationRecipeShare)
        .where(
            CreationRecipeShare.recipe_id == int(recipe_id),
            CreationRecipeShare.status == "active",
        )
        .values(status="revoked", revoked_at=revoked_at)
    )
    return int(result.rowcount or 0)


def _invalidate_moderation(db: Session, row: CreationRecipe) -> int:
    row.moderation_status = "draft"
    row.approved_version = None
    row.submitted_at = None
    row.reviewed_at = None
    row.reviewed_by = None
    row.review_note = None
    return _revoke_active_shares(db, int(row.id))


def _share_is_active(row: CreationRecipeShare) -> bool:
    return recipe_usage.share_is_active(row)


def _share_url(slug: str) -> str:
    return f"{settings.payment_frontend_base_url.rstrip('/')}/recipes/shared/{slug}"


def _serialize_share(row: CreationRecipeShare) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "recipe_id": int(row.recipe_id),
        "version": int(row.version),
        "slug": row.slug,
        "status": row.status,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "share_url": _share_url(row.slug),
        "created_at": row.created_at,
    }


def _share_by_slug(
    db: Session,
    slug: str,
    *,
    recipe_id: int | None = None,
    version: int | None = None,
) -> CreationRecipeShare:
    return recipe_usage.share_by_slug(
        db,
        slug,
        recipe_id=recipe_id,
        version=version,
    )


def _serialize_version(
    version: CreationRecipeVersion | None,
    *,
    public: bool = False,
) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": int(version.id),
        "recipe_id": int(version.recipe_id),
        "version": int(version.version),
        "schema_version": version.schema_version,
        "payload": (
            _public_recipe_payload(version.payload)
            if public
            else version.payload
        ),
        "metadata_snapshot": (
            {} if public else deepcopy(version.metadata_snapshot or {})
        ),
        "created_at": version.created_at,
    }


def _empty_usage_stats() -> dict[str, Any]:
    return {"total": 0, "unique_users": 0, "by_event": {}, "last_used_at": None}


def _usage_stats_map(db: Session, recipe_ids: list[int]) -> dict[int, dict[str, Any]]:
    """批量聚合配方使用埋点，列表页一次两条分组查询，避免逐行 N+1。"""
    stats: dict[int, dict[str, Any]] = {int(rid): _empty_usage_stats() for rid in recipe_ids}
    if not stats:
        return stats
    ids = list(stats)
    totals = db.execute(
        select(
            CreationRecipeUsageEvent.recipe_id,
            func.count(CreationRecipeUsageEvent.id),
            func.count(func.distinct(CreationRecipeUsageEvent.user_id)),
            func.max(CreationRecipeUsageEvent.created_at),
        )
        .where(CreationRecipeUsageEvent.recipe_id.in_(ids))
        .group_by(CreationRecipeUsageEvent.recipe_id)
    ).all()
    for recipe_id, total, unique_users, last_used_at in totals:
        stats[int(recipe_id)].update(
            total=int(total or 0),
            unique_users=int(unique_users or 0),
            last_used_at=last_used_at,
        )
    grouped = db.execute(
        select(
            CreationRecipeUsageEvent.recipe_id,
            CreationRecipeUsageEvent.event_type,
            func.count(CreationRecipeUsageEvent.id),
        )
        .where(CreationRecipeUsageEvent.recipe_id.in_(ids))
        .group_by(CreationRecipeUsageEvent.recipe_id, CreationRecipeUsageEvent.event_type)
    ).all()
    for recipe_id, event_type, count in grouped:
        stats[int(recipe_id)]["by_event"][str(event_type)] = int(count)
    return stats


def _serialize(
    db: Session,
    row: CreationRecipe,
    *,
    public: bool = False,
    version: CreationRecipeVersion | None = None,
    usage: dict[str, Any] | None = None,
) -> dict:
    version = version or _current_version(db, row)
    if usage is None:
        usage = _usage_stats_map(db, [int(row.id)])[int(row.id)]
    return {
        "id": int(row.id),
        "source_operation_id": None if public else row.source_operation_id,
        "title": _public_recipe_title(row.title) if public else row.title,
        "category": row.category,
        "visibility": row.visibility,
        "moderation_status": row.moderation_status,
        "favorite": False if public else bool(row.favorite),
        "current_version": int(version.version if public and version else row.current_version),
        "approved_version": row.approved_version,
        "cover_asset_url": None if public else row.cover_asset_url,
        "submitted_at": None if public else row.submitted_at,
        "reviewed_at": row.reviewed_at,
        "review_note": None if public else row.review_note,
        "version": _serialize_version(version, public=public),
        "usage": usage,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _version(db: Session, row: CreationRecipe, version: int) -> CreationRecipeVersion:
    version_row = db.execute(select(CreationRecipeVersion).where(
        CreationRecipeVersion.recipe_id == row.id,
        CreationRecipeVersion.version == version,
    )).scalar_one_or_none()
    if version_row is None:
        raise HTTPException(404, "创作配方版本不存在")
    return version_row


def _public_recipe(db: Session, recipe_id: int) -> CreationRecipe:
    row = db.execute(select(CreationRecipe).where(
        CreationRecipe.id == recipe_id,
        CreationRecipe.deleted_at.is_(None),
        CreationRecipe.visibility == "public",
        CreationRecipe.moderation_status == "approved",
        CreationRecipe.approved_version == CreationRecipe.current_version,
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "公开创作配方不存在")
    return row


def _resolve_usage_access(
    db: Session,
    row: CreationRecipe,
    user: User,
    body: CreationRecipeUsageIn,
) -> tuple[CreationRecipeVersion, Literal["owner", "public", "share"], int | None]:
    attribution, version = recipe_usage.resolve_recipe_attribution(
        db,
        user_id=int(user.id),
        recipe_id=int(row.id),
        version=body.version,
        share_slug=body.share_slug,
    )
    return version, attribution.source, attribution.share_id


def _record_usage(
    db: Session,
    *,
    recipe_id: int,
    recipe_version: int,
    user_id: int,
    event_type: str,
    source: str,
    share_id: int | None = None,
    derived_recipe_id: int | None = None,
    generation_task_id: int | None = None,
    client_event_id: str | None = None,
    context: dict[str, Any] | None = None,
    commit: bool = True,
) -> CreationRecipeUsageEvent:
    return recipe_usage.record_usage(
        db,
        recipe_id=recipe_id,
        recipe_version=recipe_version,
        user_id=user_id,
        event_type=event_type,
        source=source,
        share_id=share_id,
        derived_recipe_id=derived_recipe_id,
        generation_task_id=generation_task_id,
        client_event_id=client_event_id,
        context=context,
        commit=commit,
    )


@router.get("", response_model=list[CreationRecipeOut])
def list_recipes(
    category: str | None = None,
    favorite: bool | None = None,
    q: str = Query(default="", max_length=128),
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(CreationRecipe).where(
        CreationRecipe.user_id == user.id,
        CreationRecipe.deleted_at.is_(None),
    )
    if category in {"image", "video"}:
        query = query.where(CreationRecipe.category == category)
    if favorite is not None:
        query = query.where(CreationRecipe.favorite.is_(bool(favorite)))
    search = q.strip()
    if search:
        query = query.where(CreationRecipe.title.ilike(f"%{search}%"))
    rows = list(db.execute(
        query.order_by(
            CreationRecipe.favorite.desc(),
            CreationRecipe.updated_at.desc(),
            CreationRecipe.id.desc(),
        )
        .limit(min(max(int(limit), 1), 200))
        .offset(max(int(offset), 0))
    ).scalars())
    usage_stats = _usage_stats_map(db, [int(row.id) for row in rows])
    return [_serialize(db, row, usage=usage_stats[int(row.id)]) for row in rows]


@router.post("", response_model=CreationRecipeOut)
def create_recipe(
    body: CreationRecipeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    payload = _with_catalog_version_snapshot(db, body.payload)
    try:
        cover_asset_url = _normalize_cover_url(body.cover_asset_url)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    assert_text_allowed(db, body.title, cover_asset_url, payload)
    if body.source_operation_id is not None:
        operation = db.get(ReverseOperation, body.source_operation_id)
        if operation is None or int(operation.user_id) != int(user.id):
            raise HTTPException(404, "来源反推任务不存在")
        if operation.status != "succeeded":
            raise HTTPException(409, "来源反推任务尚未成功")
        if operation.target in {"image", "video"} and operation.target != body.category:
            raise HTTPException(422, "配方类型与来源反推任务不一致")
    row = CreationRecipe(
        user_id=user.id,
        source_operation_id=body.source_operation_id,
        title=body.title,
        category=body.category,
        visibility=body.visibility,
        moderation_status="draft",
        favorite=body.favorite,
        current_version=1,
        cover_asset_url=cover_asset_url,
    )
    db.add(row)
    db.flush()
    db.add(CreationRecipeVersion(
        recipe_id=row.id,
        version=1,
        schema_version="creation-recipe.v1",
        payload=payload,
        metadata_snapshot=_recipe_version_metadata(row),
    ))
    _audit_recipe_change(
        db,
        request,
        user,
        "create_creation_recipe",
        row,
        detail={"version": 1},
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.get("/public", response_model=list[CreationRecipeOut])
def list_public_recipes(
    category: str | None = None,
    q: str = Query(default="", max_length=128),
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    query = select(CreationRecipe).where(
        CreationRecipe.deleted_at.is_(None),
        CreationRecipe.visibility == "public",
        CreationRecipe.moderation_status == "approved",
        CreationRecipe.approved_version == CreationRecipe.current_version,
    )
    if category in {"image", "video"}:
        query = query.where(CreationRecipe.category == category)
    search = q.strip()
    if search:
        query = query.where(CreationRecipe.title.ilike(f"%{search}%"))
    rows = list(db.execute(
        query.order_by(CreationRecipe.updated_at.desc(), CreationRecipe.id.desc())
        .limit(min(max(int(limit), 1), 200))
        .offset(max(int(offset), 0))
    ).scalars())
    usage_stats = _usage_stats_map(db, [int(row.id) for row in rows])
    return [
        _serialize(db, row, public=True, usage=usage_stats[int(row.id)])
        for row in rows
    ]


@router.get("/public/{recipe_id}", response_model=CreationRecipeOut)
def get_public_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
):
    return _serialize(db, _public_recipe(db, recipe_id), public=True)


@router.get("/shared/{slug}", response_model=CreationRecipeSharedOut)
def get_shared_recipe(
    slug: str,
    db: Session = Depends(get_db),
):
    share = _share_by_slug(db, slug)
    row = db.get(CreationRecipe, share.recipe_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(404, "配方分享不存在或已失效")
    version = _version(db, row, int(share.version))
    return {
        "share": _serialize_share(share),
        "recipe": _serialize(db, row, public=True, version=version),
    }


@router.get("/{recipe_id}", response_model=CreationRecipeOut)
def get_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _serialize(db, _owned_recipe(db, recipe_id, user.id))


@router.patch("/{recipe_id}", response_model=CreationRecipeOut)
def update_recipe_metadata(
    recipe_id: int,
    body: CreationRecipeMetadataPatch,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    before = _recipe_audit_snapshot(row)
    before_metadata = _recipe_version_metadata(row)
    title = body.title if "title" in body.model_fields_set else row.title
    visibility = (
        body.visibility if "visibility" in body.model_fields_set else row.visibility
    )
    cover_asset_url = (
        body.cover_asset_url
        if "cover_asset_url" in body.model_fields_set
        else row.cover_asset_url
    )
    if visibility == "public":
        version_row = _current_version(db, row)
        if version_row is None:
            raise HTTPException(409, "当前配方版本不存在，无法公开")
        assert_text_allowed(db, title, cover_asset_url, version_row.payload)
    else:
        assert_text_allowed(db, title, cover_asset_url)
    changed_fields = sorted(
        field
        for field in body.model_fields_set
        if {
            "title": title,
            "visibility": visibility,
            "cover_asset_url": cover_asset_url,
        }[field]
        != before_metadata[field]
    )
    if not changed_fields:
        return _serialize(db, row)
    row.title = title
    row.visibility = visibility
    row.cover_asset_url = cover_asset_url
    current = _current_version(db, row)
    if current is None:
        raise HTTPException(409, "当前配方版本不存在，无法修改元数据")
    latest_version = db.scalar(
        select(func.max(CreationRecipeVersion.version)).where(
            CreationRecipeVersion.recipe_id == row.id,
        )
    )
    next_version = int(latest_version or 0) + 1
    db.add(CreationRecipeVersion(
        recipe_id=row.id,
        version=next_version,
        schema_version=current.schema_version,
        payload=deepcopy(current.payload),
        metadata_snapshot=_recipe_version_metadata(row),
    ))
    row.current_version = next_version
    revoked_share_count = _invalidate_moderation(db, row)
    row.updated_at = datetime.now(timezone.utc)
    _audit_recipe_change(
        db,
        request,
        user,
        "update_creation_recipe_metadata",
        row,
        detail={
            "changed_fields": changed_fields,
            "before": before,
            "version": next_version,
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.post("/{recipe_id}/submit-review", response_model=CreationRecipeOut)
def submit_recipe_review(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    if row.visibility != "public":
        raise HTTPException(409, "请先将创作配方设为公开候选")
    if row.moderation_status == "pending" or _moderation_is_current(row):
        return _serialize(db, row)
    version = _current_version(db, row)
    if version is None:
        raise HTTPException(409, "当前配方版本不存在，无法提交审核")
    assert_text_allowed(db, row.title, row.cover_asset_url, version.payload)
    now = datetime.now(timezone.utc)
    row.moderation_status = "pending"
    row.approved_version = None
    row.submitted_at = now
    row.reviewed_at = None
    row.reviewed_by = None
    row.review_note = None
    row.updated_at = now
    revoked_share_count = _revoke_active_shares(db, int(row.id), now=now)
    _audit_recipe_change(
        db,
        request,
        user,
        "submit_creation_recipe_review",
        row,
        detail={
            "version": int(row.current_version),
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.get("/{recipe_id}/shares", response_model=list[CreationRecipeShareOut])
def list_recipe_shares(
    recipe_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    shares = list(
        db.execute(
            select(CreationRecipeShare)
            .where(CreationRecipeShare.recipe_id == row.id)
            .order_by(CreationRecipeShare.id.desc())
        ).scalars()
    )
    return [_serialize_share(share) for share in shares]


@router.post("/{recipe_id}/shares", response_model=CreationRecipeShareOut)
def create_recipe_share(
    recipe_id: int,
    body: CreationRecipeShareIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    if not _moderation_is_current(row):
        raise HTTPException(409, "配方当前版本尚未审核通过，无法创建公开分享")
    version = int(body.version or row.current_version)
    if (
        version != int(row.current_version)
        or version != int(row.approved_version or 0)
    ):
        raise HTTPException(409, "仅可分享当前已审核通过的配方版本")
    version_row = _version(db, row, version)
    assert_text_allowed(db, row.title, row.cover_asset_url, version_row.payload)
    slug = secrets.token_urlsafe(24)
    share = CreationRecipeShare(
        recipe_id=row.id,
        owner_user_id=user.id,
        version=version,
        slug=slug,
        status="active",
        expires_at=body.expires_at,
    )
    db.add(share)
    db.flush()
    _audit_recipe_change(
        db,
        request,
        user,
        "create_creation_recipe_share",
        row,
        detail={
            "share_id": int(share.id),
            "version": int(share.version),
            "expires": share.expires_at is not None,
        },
    )
    db.commit()
    db.refresh(share)
    return _serialize_share(share)


@router.delete(
    "/{recipe_id}/shares/{share_id}",
    response_model=CreationRecipeShareOut,
)
def revoke_recipe_share(
    recipe_id: int,
    share_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    share = db.execute(
        select(CreationRecipeShare).where(
            CreationRecipeShare.id == share_id,
            CreationRecipeShare.recipe_id == row.id,
            CreationRecipeShare.owner_user_id == user.id,
        )
    ).scalar_one_or_none()
    if share is None:
        raise HTTPException(404, "配方分享不存在")
    if share.status == "active":
        share.status = "revoked"
        share.revoked_at = datetime.now(timezone.utc)
        _audit_recipe_change(
            db,
            request,
            user,
            "revoke_creation_recipe_share",
            row,
            detail={"share_id": int(share.id), "version": int(share.version)},
        )
        db.commit()
        db.refresh(share)
    return _serialize_share(share)


@router.post("/{recipe_id}/usage", response_model=CreationRecipeUsageEventOut)
def record_recipe_usage(
    recipe_id: int,
    body: CreationRecipeUsageIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.get(CreationRecipe, recipe_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(404, "创作配方不存在")
    version, source, share_id = _resolve_usage_access(db, row, user, body)
    if body.generation_task_id is not None:
        task = db.get(GenTask, body.generation_task_id)
        if task is None or int(task.user_id) != int(user.id):
            raise HTTPException(404, "关联生成任务不存在")
        if task.category != row.category:
            raise HTTPException(422, "生成任务类型与配方不一致")
    return _record_usage(
        db,
        recipe_id=int(row.id),
        recipe_version=int(version.version),
        user_id=int(user.id),
        event_type=body.event_type,
        source=source,
        share_id=share_id,
        generation_task_id=body.generation_task_id,
        client_event_id=body.client_event_id,
        context=body.context,
    )


@router.get("/{recipe_id}/usage", response_model=CreationRecipeUsageSummaryOut)
def recipe_usage_summary(
    recipe_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    grouped = db.execute(
        select(
            CreationRecipeUsageEvent.event_type,
            func.count(CreationRecipeUsageEvent.id),
        )
        .where(CreationRecipeUsageEvent.recipe_id == row.id)
        .group_by(CreationRecipeUsageEvent.event_type)
    ).all()
    total, unique_users, last_used_at = db.execute(
        select(
            func.count(CreationRecipeUsageEvent.id),
            func.count(func.distinct(CreationRecipeUsageEvent.user_id)),
            func.max(CreationRecipeUsageEvent.created_at),
        ).where(CreationRecipeUsageEvent.recipe_id == row.id)
    ).one()
    return {
        "recipe_id": int(row.id),
        "total": int(total or 0),
        "unique_users": int(unique_users or 0),
        "by_event": {str(event_type): int(count) for event_type, count in grouped},
        "last_used_at": last_used_at,
    }


@router.get(
    "/{recipe_id}/versions",
    response_model=list[CreationRecipeVersionOut],
)
def list_recipe_versions(
    recipe_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    return list(db.execute(
        select(CreationRecipeVersion)
        .where(CreationRecipeVersion.recipe_id == row.id)
        .order_by(CreationRecipeVersion.version.desc())
    ).scalars())


@router.get(
    "/{recipe_id}/versions/{version}",
    response_model=CreationRecipeVersionOut,
)
def get_recipe_version(
    recipe_id: int,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    return _version(db, row, version)


@router.post("/{recipe_id}/versions", response_model=CreationRecipeVersionOut)
def create_recipe_version(
    recipe_id: int,
    body: CreationRecipeVersionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    payload = _with_catalog_version_snapshot(db, body.payload)
    try:
        validate_creation_recipe_payload(payload, category=row.category)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    assert_text_allowed(db, payload)
    latest_version = db.execute(
        select(func.max(CreationRecipeVersion.version)).where(
            CreationRecipeVersion.recipe_id == row.id,
        )
    ).scalar_one()
    next_version = int(latest_version or 0) + 1
    version_row = CreationRecipeVersion(
        recipe_id=row.id,
        version=next_version,
        schema_version="creation-recipe.v1",
        payload=payload,
        metadata_snapshot=_recipe_version_metadata(row),
    )
    db.add(version_row)
    row.current_version = next_version
    revoked_share_count = _invalidate_moderation(db, row)
    row.updated_at = datetime.now(timezone.utc)
    db.flush()
    _audit_recipe_change(
        db,
        request,
        user,
        "create_creation_recipe_version",
        row,
        detail={
            "version": next_version,
            "version_id": int(version_row.id),
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    db.refresh(version_row)
    return version_row


@router.post(
    "/{recipe_id}/versions/{version}/activate",
    response_model=CreationRecipeOut,
)
def activate_recipe_version(
    recipe_id: int,
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    previous_version = int(row.current_version)
    version_row = _version(db, row, version)
    _apply_recipe_version_metadata(row, version_row)
    if row.visibility == "public":
        assert_text_allowed(db, row.title, row.cover_asset_url, version_row.payload)
    row.current_version = version_row.version
    revoked_share_count = _invalidate_moderation(db, row)
    row.updated_at = datetime.now(timezone.utc)
    _audit_recipe_change(
        db,
        request,
        user,
        "activate_creation_recipe_version",
        row,
        detail={
            "previous_version": previous_version,
            "activated_version": int(version_row.version),
            "version_id": int(version_row.id),
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.post("/{recipe_id}/clone", response_model=CreationRecipeOut)
def clone_recipe(
    recipe_id: int,
    body: CreationRecipeCloneIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = db.execute(
        select(CreationRecipe)
        .where(
            CreationRecipe.id == recipe_id,
            CreationRecipe.deleted_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(404, "创作配方不存在")
    is_owner = int(source.user_id) == int(user.id)
    share: CreationRecipeShare | None = None
    if body.share_slug:
        share = _share_by_slug(db, body.share_slug, recipe_id=int(source.id))
    selected_version = int(body.version or (share.version if share else source.current_version))
    if share is not None and selected_version != int(share.version):
        raise HTTPException(404, "分享的配方版本不存在")
    if not is_owner and share is None:
        if not _moderation_is_current(source):
            raise HTTPException(404, "创作配方不存在")
        if selected_version != int(source.current_version):
            raise HTTPException(404, "公开创作配方版本不存在")
    source_version = _version(db, source, selected_version)
    payload = (
        deepcopy(source_version.payload)
        if is_owner
        else _public_recipe_payload(source_version.payload)
    )
    payload["derived_from_recipe_id"] = int(source.id)
    payload["derived_from_recipe_version"] = int(source_version.version)
    title = body.title or f"{source.title} 副本"
    if len(title) > 128:
        title = f"{source.title[:123].rstrip()} 副本"
    cover_asset_url = (
        source.cover_asset_url
        if is_owner
        else None
    )
    assert_text_allowed(db, title, cover_asset_url, payload)
    cloned = CreationRecipe(
        user_id=user.id,
        source_operation_id=None,
        title=title,
        category=source.category,
        visibility="private",
        moderation_status="draft",
        favorite=False,
        current_version=1,
        cover_asset_url=cover_asset_url,
    )
    db.add(cloned)
    db.flush()
    db.add(CreationRecipeVersion(
        recipe_id=cloned.id,
        version=1,
        schema_version=source_version.schema_version,
        payload=payload,
        metadata_snapshot=_recipe_version_metadata(cloned),
    ))
    _record_usage(
        db,
        recipe_id=int(source.id),
        recipe_version=int(source_version.version),
        user_id=int(user.id),
        event_type="clone",
        source=("owner" if is_owner and share is None else "share" if share else "public"),
        share_id=int(share.id) if share else None,
        derived_recipe_id=int(cloned.id),
        context={"derived_title": title},
        commit=False,
    )
    _audit_recipe_change(
        db,
        request,
        user,
        "clone_creation_recipe",
        cloned,
        detail={
            "source_recipe_id": int(source.id),
            "source_version": int(source_version.version),
            "source": (
                "owner" if is_owner and share is None else "share" if share else "public"
            ),
            "share_id": int(share.id) if share else None,
        },
    )
    db.commit()
    db.refresh(cloned)
    return _serialize(db, cloned)


@router.post("/{recipe_id}/favorite", response_model=CreationRecipeOut)
def toggle_recipe_favorite(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    previous_favorite = bool(row.favorite)
    row.favorite = not bool(row.favorite)
    row.updated_at = datetime.now(timezone.utc)
    _audit_recipe_change(
        db,
        request,
        user,
        "toggle_creation_recipe_favorite",
        row,
        detail={
            "before": previous_favorite,
            "after": bool(row.favorite),
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.delete("/{recipe_id}")
def delete_recipe(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    version_count = int(db.scalar(
        select(func.count(CreationRecipeVersion.id)).where(
            CreationRecipeVersion.recipe_id == row.id
        )
    ) or 0)
    share_count = int(db.scalar(
        select(func.count(CreationRecipeShare.id)).where(
            CreationRecipeShare.recipe_id == row.id
        )
    ) or 0)
    usage_count = int(db.scalar(
        select(func.count(CreationRecipeUsageEvent.id)).where(
            CreationRecipeUsageEvent.recipe_id == row.id
        )
    ) or 0)
    revoked_share_count = _revoke_active_shares(db, int(row.id))
    row.deleted_at = datetime.now(timezone.utc)
    row.updated_at = row.deleted_at
    _audit_recipe_change(
        db,
        request,
        user,
        "delete_creation_recipe",
        row,
        detail={
            "version_count": version_count,
            "share_count": share_count,
            "usage_count": usage_count,
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    return {"ok": True}
