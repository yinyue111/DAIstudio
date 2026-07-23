"""Unified generated/uploaded asset library for the current user."""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AssetFolder,
    AssetFolderItem,
    GenAsset,
    GenTask,
    UploadedAsset,
    UserAssetMetadata,
)
from . import retention, storage, upload_quota
from .asset_output import to_asset_out
from .media_sidecars import (
    direct_keys_for_asset_urls,
    related_model_ref_keys,
    storage_bytes_for_asset_urls,
    storage_bytes_for_keys,
    unlink_keys,
)

_UPLOAD_ROOT_PREFIXES = ("upload/", "upload_video/")
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CURSOR_VERSION = 1
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class InvalidAssetRef(ValueError):
    pass


class InvalidCursor(ValueError):
    pass


class AssetNotFound(LookupError):
    pass


class AssetInUse(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedAsset:
    asset_ref: str
    origin: str
    row: GenAsset | UploadedAsset
    related_upload_rows: tuple[UploadedAsset, ...] = ()


@dataclass(frozen=True)
class _ListEntry:
    sort_key: tuple[int, int, str]
    item: dict


def _uploaded_asset_origin(row: UploadedAsset) -> str:
    return "fetched" if row.origin == "fetched" else "uploaded"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _timestamp_micros(value: datetime | None) -> int:
    aware = _aware(value)
    if aware is None:
        return 0
    delta = aware.astimezone(timezone.utc) - _UTC_EPOCH
    return ((delta.days * 86400 + delta.seconds) * 1_000_000) + delta.microseconds


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(token: str) -> bytes:
    if not token or len(token) > 1024 or not _B64_RE.fullmatch(token):
        raise InvalidAssetRef("asset_ref 非法")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, binascii.Error):
        raise InvalidAssetRef("asset_ref 非法") from None
    if _b64_encode(raw) != token:
        raise InvalidAssetRef("asset_ref 非法")
    return raw


def generated_asset_ref(asset_id: int) -> str:
    return f"g.{int(asset_id)}"


def uploaded_asset_ref(storage_key: str) -> str:
    if not _is_upload_root(storage_key):
        raise InvalidAssetRef("asset_ref 非法")
    return f"u.{_b64_encode(storage_key.encode('utf-8'))}"


def _is_upload_root(storage_key: str) -> bool:
    if not storage_key.startswith(_UPLOAD_ROOT_PREFIXES):
        return False
    _, _, filename = storage_key.partition("/")
    return bool(filename and "/" not in filename and filename not in {".", ".."})


def _parse_asset_ref(asset_ref: str) -> tuple[str, int | str]:
    value = str(asset_ref or "").strip()
    if value.startswith("g."):
        raw_id = value[2:]
        if not raw_id.isdigit() or raw_id.startswith("0"):
            raise InvalidAssetRef("asset_ref 非法")
        asset_id = int(raw_id)
        if asset_id <= 0 or generated_asset_ref(asset_id) != value:
            raise InvalidAssetRef("asset_ref 非法")
        return "generated", asset_id
    if value.startswith("u."):
        try:
            key = _b64_decode(value[2:]).decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidAssetRef("asset_ref 非法") from None
        if (
            len(key) > 255
            or not _is_upload_root(key)
            or uploaded_asset_ref(key) != value
        ):
            raise InvalidAssetRef("asset_ref 非法")
        return "uploaded", key
    raise InvalidAssetRef("asset_ref 非法")


def resolve_asset_refs(db: Session, user_id: int, asset_refs: list[str]) -> list[ResolvedAsset]:
    parsed = [(asset_ref, *_parse_asset_ref(asset_ref)) for asset_ref in asset_refs]
    generated_ids = [value for _, origin, value in parsed if origin == "generated"]
    upload_keys = [value for _, origin, value in parsed if origin == "uploaded"]

    generated_rows = {
        int(row.id): row
        for row in db.execute(
            select(GenAsset).where(
                GenAsset.user_id == user_id,
                GenAsset.id.in_(generated_ids),
            )
        ).scalars()
    } if generated_ids else {}
    upload_roots = {
        row.key: row
        for row in db.execute(
            select(UploadedAsset).where(
                UploadedAsset.user_id == user_id,
                UploadedAsset.key.in_(upload_keys),
            )
        ).scalars()
    } if upload_keys else {}

    related_keys = {
        key
        for key in upload_keys
        for key in retention.upload_related_keys(str(key))
    }
    upload_groups: dict[str, list[UploadedAsset]] = {str(key): [] for key in upload_keys}
    if related_keys:
        rows = list(db.execute(
            select(UploadedAsset).where(
                UploadedAsset.user_id == user_id,
                UploadedAsset.key.in_(related_keys),
            )
        ).scalars())
        by_key = {row.key: row for row in rows}
        for root_key in upload_groups:
            upload_groups[root_key] = [
                by_key[key]
                for key in retention.upload_related_keys(root_key)
                if key in by_key
            ]

    resolved: list[ResolvedAsset] = []
    for asset_ref, origin, value in parsed:
        if origin == "generated":
            row = generated_rows.get(int(value))
            if row is None:
                raise AssetNotFound("资产不存在")
            resolved.append(ResolvedAsset(asset_ref, origin, row))
            continue
        root = upload_roots.get(str(value))
        if root is None:
            raise AssetNotFound("资产不存在")
        resolved.append(
            ResolvedAsset(asset_ref, origin, root, tuple(upload_groups[str(value)]))
        )
    return resolved


def resolve_asset_ref(db: Session, user_id: int, asset_ref: str) -> ResolvedAsset:
    return resolve_asset_refs(db, user_id, [asset_ref])[0]


def _media_type(resolved: ResolvedAsset) -> str:
    if resolved.origin == "generated":
        assert isinstance(resolved.row, GenAsset)
        return resolved.row.type
    assert isinstance(resolved.row, UploadedAsset)
    return "video" if resolved.row.key.startswith("upload_video/") else "image"


def _metadata_row(
    db: Session,
    user_id: int,
    resolved: ResolvedAsset,
) -> UserAssetMetadata:
    row = db.scalar(
        select(UserAssetMetadata).where(
            UserAssetMetadata.user_id == int(user_id),
            UserAssetMetadata.asset_ref == resolved.asset_ref,
        )
    )
    media_type = _media_type(resolved)
    if row is None:
        row = UserAssetMetadata(
            user_id=int(user_id),
            asset_ref=resolved.asset_ref,
            media_type=media_type,
            tags=[],
            analysis_status="pending",
        )
        db.add(row)
        db.flush()
    elif row.media_type != media_type:
        row.media_type = media_type
        row.content_sha256 = None
        row.perceptual_hash = None
        row.perceptual_hash_algorithm = None
        row.analysis_status = "pending"
        row.analysis_error = None
        row.analyzed_at = None
    return row


def metadata_for_asset_refs(
    db: Session,
    user_id: int,
    asset_refs: list[str],
) -> dict[str, UserAssetMetadata]:
    unique_refs = list(dict.fromkeys(str(value or "").strip() for value in asset_refs))
    unique_refs = [value for value in unique_refs if value]
    if not unique_refs:
        return {}
    return {
        row.asset_ref: row
        for row in db.scalars(
            select(UserAssetMetadata).where(
                UserAssetMetadata.user_id == int(user_id),
                UserAssetMetadata.asset_ref.in_(unique_refs),
            )
        )
    }


def metadata_payload(row: UserAssetMetadata) -> dict:
    return {
        "asset_ref": row.asset_ref,
        "media_type": row.media_type,
        "tags": list(row.tags or []),
        "content_sha256": row.content_sha256,
        "perceptual_hash": row.perceptual_hash,
        "perceptual_hash_algorithm": row.perceptual_hash_algorithm,
        "analysis_status": row.analysis_status,
        "analysis_error": row.analysis_error,
        "analyzed_at": row.analyzed_at,
        "updated_at": row.updated_at,
    }


def _apply_metadata(item: dict, row: UserAssetMetadata | None) -> dict:
    item.update({
        "tags": list(row.tags or []) if row is not None else [],
        "content_sha256": row.content_sha256 if row is not None else None,
        "perceptual_hash": row.perceptual_hash if row is not None else None,
        "analysis_status": row.analysis_status if row is not None else "pending",
        "analysis_error": row.analysis_error if row is not None else None,
    })
    return item


def update_asset_tags(
    db: Session,
    user_id: int,
    asset_ref: str,
    tags: list[str],
) -> UserAssetMetadata:
    resolved = resolve_asset_ref(db, user_id, asset_ref)
    row = _metadata_row(db, user_id, resolved)
    row.tags = list(tags)
    db.commit()
    db.refresh(row)
    return row


def _storage_key(resolved: ResolvedAsset) -> str | None:
    if resolved.origin == "uploaded":
        assert isinstance(resolved.row, UploadedAsset)
        return resolved.row.key
    assert isinstance(resolved.row, GenAsset)
    for url in (resolved.row.hd_url, resolved.row.preview_url):
        key = storage.key_from_url(str(url or ""))
        if key:
            return key
    return None


def _dhash64(path: Path) -> str:
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                raise ValueError("图片尺寸无效")
            if width * height > int(settings.parse_localize_image_max_pixels):
                raise ValueError("图片像素超过分析上限")
            gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(gray.getdata())
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise ValueError("图片像素无法解码") from error
    digest = 0
    for row_index in range(8):
        offset = row_index * 9
        for column in range(8):
            digest = (digest << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return f"{digest:016x}"


def analyze_asset(
    db: Session,
    user_id: int,
    asset_ref: str,
    *,
    force: bool = False,
    commit: bool = True,
) -> UserAssetMetadata:
    resolved = resolve_asset_ref(db, user_id, asset_ref)
    row = _metadata_row(db, user_id, resolved)
    if (
        not force
        and row.content_sha256
        and row.analysis_status == "ready"
        and (row.media_type == "video" or row.perceptual_hash)
    ):
        return row

    row.content_sha256 = None
    row.perceptual_hash = None
    row.perceptual_hash_algorithm = None
    row.analysis_error = None
    row.analyzed_at = datetime.now(timezone.utc)
    key = _storage_key(resolved)
    if not key:
        row.analysis_status = "degraded"
        row.analysis_error = "素材不在平台受控存储中，无法计算真实内容 hash"
    else:
        try:
            content_hash = storage.object_sha256(key)
            if not content_hash:
                raise FileNotFoundError("存储对象不存在")
            row.content_sha256 = content_hash
            if row.media_type == "image":
                path = storage.local_path(key)
                if not path.exists() or not path.is_file():
                    raise FileNotFoundError("图片原文件不存在")
                row.perceptual_hash = _dhash64(path)
                row.perceptual_hash_algorithm = "dhash64"
            row.analysis_status = "ready"
        except Exception as error:  # noqa: BLE001
            row.analysis_status = "degraded"
            row.analysis_error = (str(error).strip() or "素材分析失败")[:500]
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def _hamming_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def find_similar_assets(
    db: Session,
    user_id: int,
    query_asset_ref: str,
    candidate_asset_refs: list[str],
    *,
    max_distance: int = 8,
) -> dict:
    unique_candidates = list(dict.fromkeys(
        str(value or "").strip()
        for value in candidate_asset_refs
        if str(value or "").strip() and str(value or "").strip() != query_asset_ref
    ))
    query = analyze_asset(db, user_id, query_asset_ref, commit=False)
    candidates: list[UserAssetMetadata] = []
    degraded_refs: list[str] = []
    for asset_ref in unique_candidates:
        try:
            candidate = analyze_asset(db, user_id, asset_ref, commit=False)
        except (InvalidAssetRef, AssetNotFound):
            degraded_refs.append(asset_ref)
            continue
        candidates.append(candidate)
        if candidate.analysis_status == "degraded":
            degraded_refs.append(asset_ref)
    db.commit()
    db.refresh(query)
    for candidate in candidates:
        db.refresh(candidate)

    exact_available = bool(query.content_sha256)
    perceptual_available = bool(query.media_type == "image" and query.perceptual_hash)
    matches: list[dict] = []
    matched_refs: list[str] = []
    for candidate in candidates:
        if candidate.media_type != query.media_type:
            continue
        if exact_available and candidate.content_sha256 == query.content_sha256:
            matches.append({
                "asset_ref": candidate.asset_ref,
                "match_type": "exact",
                "hamming_distance": 0 if perceptual_available and candidate.perceptual_hash else None,
                "metadata": metadata_payload(candidate),
            })
            matched_refs.append(candidate.asset_ref)
            continue
        if not perceptual_available or not candidate.perceptual_hash:
            continue
        distance = _hamming_distance(query.perceptual_hash, candidate.perceptual_hash)
        if distance <= max(0, min(64, int(max_distance))):
            matches.append({
                "asset_ref": candidate.asset_ref,
                "match_type": "similar",
                "hamming_distance": distance,
                "metadata": metadata_payload(candidate),
            })
            matched_refs.append(candidate.asset_ref)
    asset_views = asset_items_for_refs(db, user_id, matched_refs)
    for match in matches:
        match["asset"] = asset_views.get(match["asset_ref"])

    messages: list[str] = []
    if query.analysis_status == "degraded":
        messages.append(query.analysis_error or "查询素材无法完整分析")
    if query.media_type == "video":
        messages.append("视频已执行 SHA-256 精确重复检测，暂不支持感知相似分析")
    elif not perceptual_available:
        messages.append("图片感知 hash 不可用，当前仅能执行精确重复检测")
    if degraded_refs:
        messages.append(f"{len(degraded_refs)} 个候选素材无法真实分析")
    status = "degraded" if messages else "ready"
    return {
        "status": status,
        "query_asset_ref": query_asset_ref,
        "exact_available": exact_available,
        "perceptual_available": perceptual_available,
        "message": "；".join(dict.fromkeys(messages)) or None,
        "query": metadata_payload(query),
        "matches": matches,
        "degraded_asset_refs": degraded_refs,
    }


def _cursor_encode(sort_key: tuple[int, int, str]) -> str:
    payload = [_CURSOR_VERSION, sort_key[0], sort_key[1], sort_key[2]]
    return _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _cursor_decode(cursor: str) -> tuple[int, int, str]:
    try:
        if not cursor or len(cursor) > 2048 or not _B64_RE.fullmatch(cursor):
            raise InvalidCursor("游标无效")
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded)
        if _b64_encode(raw) != cursor:
            raise InvalidCursor("游标无效")
        payload = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(payload, list)
            or len(payload) != 4
            or payload[0] != _CURSOR_VERSION
            or isinstance(payload[1], bool)
            or not isinstance(payload[1], int)
            or payload[1] < 0
            or payload[2] not in (0, 1)
            or not isinstance(payload[3], str)
            or not payload[3]
            or len(payload[3]) > 512
        ):
            raise InvalidCursor("游标无效")
        return payload[1], payload[2], payload[3]
    except (InvalidCursor, ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        raise InvalidCursor("游标无效") from None


def _retention_fields(
    created_at: datetime | None,
    retained_at: datetime | None,
    retention_days: int,
    now: datetime,
) -> tuple[datetime | None, int | None]:
    if retained_at is not None:
        return None, None
    created = _aware(created_at)
    if created is None:
        return None, None
    expires_at = created + timedelta(days=retention_days)
    return expires_at, max(0, (expires_at - now).days)


def _matches_filters(
    *,
    origin: str,
    asset_type: str,
    favorite_value: bool,
    retained_value: bool,
    origin_filter: str,
    type_filter: str,
    favorite_filter: bool | None,
    retention_filter: str,
) -> bool:
    return (
        (origin_filter == "all" or origin == origin_filter)
        and (type_filter == "all" or asset_type == type_filter)
        and (favorite_filter is None or favorite_value is favorite_filter)
        and (
            retention_filter == "all"
            or (retention_filter == "retained" and retained_value)
            or (retention_filter == "expiring" and not retained_value)
        )
    )


def list_user_assets(
    db: Session,
    user_id: int,
    *,
    origin: str,
    asset_type: str,
    favorite: bool | None,
    retention_filter: str,
    search_query: str | None = None,
    tag_filter: str | None = None,
    folder_filter: str = "all",
    limit: int,
    offset: int,
    cursor: str | None,
) -> dict:
    now = datetime.now(timezone.utc)
    retention_days = retention.get_retention_days(db)
    cutoff = now - timedelta(days=retention_days)
    entries: list[_ListEntry] = []

    if origin in ("all", "generated"):
        generated_rows = list(db.execute(
            select(GenAsset).where(
                GenAsset.user_id == user_id,
                or_(GenAsset.created_at >= cutoff, GenAsset.retained_at.is_not(None)),
            )
        ).scalars())
        task_ids = {int(row.task_id) for row in generated_rows if row.task_id}
        task_map = {
            int(task.id): task
            for task in db.execute(select(GenTask).where(GenTask.id.in_(task_ids))).scalars()
        } if task_ids else {}
        for row in generated_rows:
            retained_value = row.retained_at is not None
            if not _matches_filters(
                origin="generated",
                asset_type=row.type,
                favorite_value=bool(row.favorite),
                retained_value=retained_value,
                origin_filter=origin,
                type_filter=asset_type,
                favorite_filter=favorite,
                retention_filter=retention_filter,
            ):
                continue
            out = to_asset_out(db, row, task=task_map.get(int(row.task_id or 0)))
            expires_at, days_left = _retention_fields(
                row.created_at, row.retained_at, retention_days, now
            )
            asset_ref = generated_asset_ref(row.id)
            displayed_url = out.hd_url or out.preview_url
            bytes_value = row.bytes
            if bytes_value is None:
                bytes_value = storage_bytes_for_asset_urls(row.preview_url, row.hd_url)
            source_key = f"{int(row.id):020d}"
            entries.append(_ListEntry(
                sort_key=(_timestamp_micros(row.created_at), 1, source_key),
                item={
                    "asset_ref": asset_ref,
                    "origin": "generated",
                    "type": row.type,
                    "url": displayed_url,
                    "preview_url": out.preview_url,
                    "thumb": out.preview_url,
                    "favorite": bool(row.favorite),
                    "retained": retained_value,
                    "moderation_status": row.moderation_status,
                    "available": bool(row.moderation_status == "active" and displayed_url),
                    "unlocked": bool(out.unlocked),
                    "unlock_cost": int(out.unlock_cost or 0),
                    "created_at": _aware(row.created_at),
                    "expires_at": expires_at,
                    "days_left": days_left,
                    "bytes": bytes_value,
                    "width": row.width,
                    "height": row.height,
                    "duration": row.duration,
                    "filename": None,
                    "task_id": row.task_id,
                    "download_url": f"/api/me/assets/download?asset_ref={asset_ref}",
                    "_search_text": json.dumps(
                        task_map.get(int(row.task_id or 0)).prompt or {},
                        ensure_ascii=False,
                        default=str,
                    ) if task_map.get(int(row.task_id or 0)) is not None else "",
                },
            ))

    if origin in ("all", "uploaded", "fetched"):
        all_upload_rows = list(db.execute(
            select(UploadedAsset).where(UploadedAsset.user_id == user_id)
        ).scalars())
        upload_by_key = {row.key: row for row in all_upload_rows}
        for row in all_upload_rows:
            if not _is_upload_root(row.key):
                continue
            retained_value = row.retained_at is not None
            created = _aware(row.created_at)
            if not retained_value and created is not None and created < cutoff:
                continue
            row_type = "video" if row.key.startswith("upload_video/") else "image"
            row_origin = _uploaded_asset_origin(row)
            if not _matches_filters(
                origin=row_origin,
                asset_type=row_type,
                favorite_value=bool(row.favorite),
                retained_value=retained_value,
                origin_filter=origin,
                type_filter=asset_type,
                favorite_filter=favorite,
                retention_filter=retention_filter,
            ):
                continue
            preview_key = None
            for related_key in retention.upload_related_keys(row.key)[1:]:
                if related_key in upload_by_key and related_key.startswith(
                    ("upload_preview/", "upload_video_preview/")
                ):
                    preview_key = related_key
                    break
            url = storage.upload_api_url(row.key)
            preview_url = storage.upload_api_url(preview_key) if preview_key else url
            expires_at, days_left = _retention_fields(
                row.created_at, row.retained_at, retention_days, now
            )
            asset_ref = uploaded_asset_ref(row.key)
            entries.append(_ListEntry(
                sort_key=(_timestamp_micros(row.created_at), 0, row.key),
                item={
                    "asset_ref": asset_ref,
                    "origin": row_origin,
                    "type": row_type,
                    "url": url,
                    "preview_url": preview_url,
                    "thumb": preview_url,
                    "favorite": bool(row.favorite),
                    "retained": retained_value,
                    "moderation_status": "active",
                    "available": bool(url),
                    "unlocked": True,
                    "unlock_cost": 0,
                    "created_at": created,
                    "expires_at": expires_at,
                    "days_left": days_left,
                    "bytes": _uploaded_group_bytes(row.key, upload_by_key),
                    "width": row.width,
                    "height": row.height,
                    "duration": row.duration,
                    "filename": row.original_filename,
                    "task_id": None,
                    "download_url": f"/api/me/assets/download?asset_ref={asset_ref}",
                    "_search_text": row.original_filename or "",
                },
            ))

    metadata_rows = metadata_for_asset_refs(
        db,
        user_id,
        [entry.item["asset_ref"] for entry in entries],
    )
    for entry in entries:
        _apply_metadata(entry.item, metadata_rows.get(entry.item["asset_ref"]))

    folder_rows = db.execute(
        select(
            AssetFolderItem.asset_ref,
            AssetFolderItem.folder_id,
            AssetFolder.name,
        )
        .join(AssetFolder, AssetFolder.id == AssetFolderItem.folder_id)
        .where(
            AssetFolderItem.user_id == int(user_id),
            AssetFolder.user_id == int(user_id),
        )
    ).all()
    folder_map = {
        str(asset_ref): (int(folder_id), str(folder_name))
        for asset_ref, folder_id, folder_name in folder_rows
    }
    for entry in entries:
        folder = folder_map.get(entry.item["asset_ref"])
        entry.item["folder_id"] = folder[0] if folder else None
        entry.item["folder_name"] = folder[1] if folder else None

    normalized_folder = str(folder_filter or "all").strip().lower()
    if normalized_folder not in {"all", "unfiled"}:
        try:
            requested_folder_id = int(normalized_folder)
        except (TypeError, ValueError):
            raise InvalidAssetRef("素材文件夹筛选非法") from None
        owned_folder = db.scalar(
            select(AssetFolder.id).where(
                AssetFolder.id == requested_folder_id,
                AssetFolder.user_id == int(user_id),
            )
        )
        if owned_folder is None:
            raise AssetNotFound("素材文件夹不存在")
        entries = [
            entry for entry in entries
            if entry.item.get("folder_id") == requested_folder_id
        ]
    elif normalized_folder == "unfiled":
        entries = [entry for entry in entries if entry.item.get("folder_id") is None]

    normalized_tag = " ".join(str(tag_filter or "").split()).strip("# ").casefold()
    if normalized_tag:
        entries = [
            entry for entry in entries
            if normalized_tag in {
                str(value or "").casefold() for value in entry.item.get("tags") or []
            }
        ]

    normalized_query = " ".join(str(search_query or "").split()).casefold()
    if normalized_query:
        entries = [
            entry for entry in entries
            if normalized_query in " ".join([
                str(entry.item.get("asset_ref") or ""),
                str(entry.item.get("filename") or ""),
                str(entry.item.get("folder_name") or ""),
                " ".join(str(value or "") for value in entry.item.get("tags") or []),
                str(entry.item.get("_search_text") or ""),
            ]).casefold()
        ]

    entries.sort(key=lambda entry: entry.sort_key, reverse=True)
    stats = {
        "generated": sum(entry.item["origin"] == "generated" for entry in entries),
        "uploaded": sum(entry.item["origin"] == "uploaded" for entry in entries),
        "fetched": sum(entry.item["origin"] == "fetched" for entry in entries),
        "images": sum(entry.item["type"] == "image" for entry in entries),
        "videos": sum(entry.item["type"] == "video" for entry in entries),
        "favorites": sum(bool(entry.item["favorite"]) for entry in entries),
        "retained": sum(bool(entry.item["retained"]) for entry in entries),
    }
    total = len(entries)
    page_offset = offset
    if cursor is not None:
        cursor_key = _cursor_decode(cursor)
        entries = [entry for entry in entries if entry.sort_key < cursor_key]
        # A cursor already identifies the next position. Treating a legacy
        # offset as cumulative here would skip a second page of mixed assets.
        page_offset = 0
    page = entries[page_offset : page_offset + limit]
    has_more = page_offset + limit < len(entries)
    for entry in page:
        entry.item.pop("_search_text", None)
    return {
        "items": [entry.item for entry in page],
        "total": total,
        "stats": stats,
        "next_cursor": _cursor_encode(page[-1].sort_key) if page and has_more else None,
    }


def update_asset_metadata(
    db: Session,
    user_id: int,
    asset_refs: list[str],
    *,
    favorite: bool | None,
    retained: bool | None,
) -> list[str]:
    rows = resolve_asset_refs(db, user_id, asset_refs)
    now = datetime.now(timezone.utc)
    generated_retention_bytes: dict[int, int] = {}
    if retained:
        for resolved in rows:
            if resolved.origin != "generated":
                continue
            asset = resolved.row
            assert isinstance(asset, GenAsset)
            if asset.retained_at is not None:
                continue
            measured = asset.bytes
            if measured is None:
                measured = storage_bytes_for_asset_urls(asset.preview_url, asset.hd_url)
            generated_retention_bytes[int(asset.id)] = max(0, int(measured or 0))
        incoming_bytes = sum(generated_retention_bytes.values())
        if incoming_bytes > 0:
            upload_quota.ensure_user_media_quota(db, user_id, incoming_bytes)
    for resolved in rows:
        if resolved.origin == "generated":
            asset = resolved.row
            assert isinstance(asset, GenAsset)
            if favorite is not None:
                asset.favorite = favorite
            if retained is not None:
                if retained and asset.retained_at is None:
                    asset.bytes = generated_retention_bytes.get(int(asset.id), asset.bytes)
                asset.retained_at = (asset.retained_at or now) if retained else None
            continue
        root = resolved.row
        assert isinstance(root, UploadedAsset)
        retained_at = (root.retained_at or now) if retained else None
        for asset in resolved.related_upload_rows:
            if favorite is not None:
                asset.favorite = favorite
            if retained is not None:
                asset.retained_at = retained_at
    db.commit()
    return [row.asset_ref for row in rows]


def _uploaded_group_bytes(
    root_key: str,
    upload_by_key: dict[str, UploadedAsset],
) -> int | None:
    total = 0
    found = False
    for key in retention.upload_related_keys(root_key):
        row = upload_by_key.get(key)
        if row is not None and row.bytes is not None:
            total += max(0, int(row.bytes))
            found = True
            continue
        stored_bytes = storage_bytes_for_keys([key])
        if stored_bytes is not None:
            total += stored_bytes
            found = True
    return total if found else None


def asset_items_for_refs(
    db: Session,
    user_id: int,
    asset_refs: list[str],
) -> dict[str, dict | None]:
    """Build complete unified-asset views for explicit, user-owned references.

    Project links may outlive retention cleanup. Missing or malformed links are
    represented as ``None`` instead of making the whole project view fail.
    """
    unique_refs = list(dict.fromkeys(str(value or "").strip() for value in asset_refs))
    unique_refs = [value for value in unique_refs if value]
    items: dict[str, dict | None] = {value: None for value in unique_refs}
    parsed: list[tuple[str, str, int | str]] = []
    for asset_ref in unique_refs:
        try:
            origin, value = _parse_asset_ref(asset_ref)
        except InvalidAssetRef:
            continue
        parsed.append((asset_ref, origin, value))

    generated_ids = [int(value) for _, origin, value in parsed if origin == "generated"]
    upload_keys = [str(value) for _, origin, value in parsed if origin == "uploaded"]
    owned_generated_ids = set(
        db.scalars(
            select(GenAsset.id).where(
                GenAsset.user_id == int(user_id),
                GenAsset.id.in_(generated_ids),
            )
        )
    ) if generated_ids else set()
    owned_upload_keys = set(
        db.scalars(
            select(UploadedAsset.key).where(
                UploadedAsset.user_id == int(user_id),
                UploadedAsset.key.in_(upload_keys),
            )
        )
    ) if upload_keys else set()
    existing_refs = [
        asset_ref
        for asset_ref, origin, value in parsed
        if (
            (origin == "generated" and int(value) in owned_generated_ids)
            or (origin == "uploaded" and str(value) in owned_upload_keys)
        )
    ]
    if not existing_refs:
        return items

    resolved = resolve_asset_refs(db, user_id, existing_refs)
    metadata_rows = metadata_for_asset_refs(db, user_id, existing_refs)
    generated_task_ids = {
        int(item.row.task_id)
        for item in resolved
        if item.origin == "generated" and isinstance(item.row, GenAsset) and item.row.task_id
    }
    task_map = {
        int(task.id): task
        for task in db.scalars(select(GenTask).where(GenTask.id.in_(generated_task_ids)))
    } if generated_task_ids else {}
    now = datetime.now(timezone.utc)
    retention_days = retention.get_retention_days(db)

    for item in resolved:
        if item.origin == "generated":
            row = item.row
            assert isinstance(row, GenAsset)
            out = to_asset_out(db, row, task=task_map.get(int(row.task_id or 0)))
            expires_at, days_left = _retention_fields(
                row.created_at,
                row.retained_at,
                retention_days,
                now,
            )
            displayed_url = out.hd_url or out.preview_url
            measured_bytes = row.bytes
            if measured_bytes is None:
                measured_bytes = storage_bytes_for_asset_urls(row.preview_url, row.hd_url)
            items[item.asset_ref] = _apply_metadata({
                "asset_ref": item.asset_ref,
                "origin": "generated",
                "type": row.type,
                "url": displayed_url,
                "preview_url": out.preview_url,
                "thumb": out.preview_url,
                "favorite": bool(row.favorite),
                "retained": row.retained_at is not None,
                "moderation_status": row.moderation_status,
                "available": bool(row.moderation_status == "active" and displayed_url),
                "unlocked": bool(out.unlocked),
                "unlock_cost": int(out.unlock_cost or 0),
                "created_at": _aware(row.created_at),
                "expires_at": expires_at,
                "days_left": days_left,
                "bytes": measured_bytes,
                "width": row.width,
                "height": row.height,
                "duration": row.duration,
                "filename": None,
                "task_id": row.task_id,
                "download_url": f"/api/me/assets/download?asset_ref={item.asset_ref}",
            }, metadata_rows.get(item.asset_ref))
            continue

        row = item.row
        assert isinstance(row, UploadedAsset)
        related_rows = {related.key: related for related in item.related_upload_rows}
        related_rows[row.key] = row
        preview_key = next(
            (
                key
                for key in retention.upload_related_keys(row.key)[1:]
                if key in related_rows
                and key.startswith(("upload_preview/", "upload_video_preview/"))
            ),
            None,
        )
        url = storage.upload_api_url(row.key)
        preview_url = storage.upload_api_url(preview_key) if preview_key else url
        expires_at, days_left = _retention_fields(
            row.created_at,
            row.retained_at,
            retention_days,
            now,
        )
        items[item.asset_ref] = _apply_metadata({
            "asset_ref": item.asset_ref,
            "origin": _uploaded_asset_origin(row),
            "type": "video" if row.key.startswith("upload_video/") else "image",
            "url": url,
            "preview_url": preview_url,
            "thumb": preview_url,
            "favorite": bool(row.favorite),
            "retained": row.retained_at is not None,
            "moderation_status": "active",
            "available": bool(url),
            "unlocked": True,
            "unlock_cost": 0,
            "created_at": _aware(row.created_at),
            "expires_at": expires_at,
            "days_left": days_left,
            "bytes": _uploaded_group_bytes(row.key, related_rows),
            "width": row.width,
            "height": row.height,
            "duration": row.duration,
            "filename": row.original_filename,
            "task_id": None,
            "download_url": f"/api/me/assets/download?asset_ref={item.asset_ref}",
        }, metadata_rows.get(item.asset_ref))
    return items


def _batch_generated_storage_keys(db: Session, assets: list[GenAsset]) -> list[str]:
    """Collect files not referenced by generated rows outside this delete batch."""
    deleting_ids = {int(asset.id) for asset in assets}
    candidates: list[str] = []
    seen: set[str] = set()
    for asset in assets:
        for key in direct_keys_for_asset_urls(asset.preview_url, asset.hd_url):
            if key in seen:
                continue
            seen.add(key)
            url = storage.public_url(key)
            referenced = bool(db.execute(
                select(GenAsset.id)
                .where(
                    GenAsset.id.not_in(deleting_ids),
                    or_(GenAsset.preview_url == url, GenAsset.hd_url == url),
                )
                .limit(1)
            ).first())
            if referenced:
                continue
            candidates.append(key)
            for sidecar_key in related_model_ref_keys(key):
                if sidecar_key not in seen:
                    seen.add(sidecar_key)
                    candidates.append(sidecar_key)
    return candidates


def delete_assets(db: Session, user_id: int, asset_refs: list[str]) -> list[str]:
    rows = resolve_asset_refs(db, user_id, asset_refs)
    candidate_urls: set[str] = set()
    for resolved in rows:
        if resolved.origin == "generated":
            asset = resolved.row
            assert isinstance(asset, GenAsset)
            candidate_urls.update(url for url in (asset.preview_url, asset.hd_url) if url)
        else:
            root = resolved.row
            assert isinstance(root, UploadedAsset)
            candidate_urls.update(
                storage.upload_api_url(key)
                for key in retention.upload_related_keys(root.key)
            )
    task_urls, studio_urls = retention.active_asset_reference_urls(
        db,
        candidate_urls,
        user_ids={user_id},
    )
    if candidate_urls.intersection(task_urls) or any(
        (user_id, url) in studio_urls for url in candidate_urls
    ):
        raise AssetInUse("资产正被生成任务或 Studio 草稿使用，无法删除")

    generated_assets = [
        resolved.row
        for resolved in rows
        if resolved.origin == "generated" and isinstance(resolved.row, GenAsset)
    ]
    unlink_after_commit: list[str] = _batch_generated_storage_keys(db, generated_assets)
    for resolved in rows:
        if resolved.origin == "generated":
            asset = resolved.row
            assert isinstance(asset, GenAsset)
            retention.detach_asset_reports(db, asset.id)
            db.delete(asset)
            continue
        for upload_row in resolved.related_upload_rows:
            unlink_after_commit.append(upload_row.key)
            db.delete(upload_row)
    db.execute(
        delete(UserAssetMetadata).where(
            UserAssetMetadata.user_id == int(user_id),
            UserAssetMetadata.asset_ref.in_([row.asset_ref for row in rows]),
        )
    )
    db.commit()
    unlink_keys(unlink_after_commit)
    return [row.asset_ref for row in rows]
