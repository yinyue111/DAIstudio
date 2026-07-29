"""Creation-recipe lifecycle helpers shared by API and service consumers."""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException, Request
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import get_client_ip
from ..models import (
    CreationRecipe,
    CreationRecipeShare,
    CreationRecipeUsageEvent,
    CreationRecipeVersion,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
    User,
)
from ..schemas import CreationRecipeUsageIn
from . import audit, recipe_usage

# Helper implementations are appended below by the mechanical extraction step.

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
