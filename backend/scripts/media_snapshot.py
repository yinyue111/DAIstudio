"""Create, verify, and restore complete local or S3 media snapshots.

The archive contains stable storage keys plus a SHA-256 manifest. Restore is a
dry run unless ``--execute`` is provided, and existing mismatched objects are
never replaced unless ``--overwrite`` is also explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.services import storage
from scripts.migrate_media_storage import iter_local_objects

SNAPSHOT_FORMAT = "ai-studio-media-snapshot"
SNAPSHOT_VERSION = 1
MANIFEST_NAME = "manifest.json"
OBJECT_PREFIX = "objects/"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class SnapshotObject:
    key: str
    size: int
    sha256: str


@dataclass
class SnapshotSummary:
    objects: int = 0
    bytes: int = 0
    restored: int = 0
    skipped: int = 0


class _HashingReader:
    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.stream.read(size)
        if chunk:
            self.digest.update(chunk)
            self.bytes_read += len(chunk)
        return chunk

    @property
    def hexdigest(self) -> str:
        return self.digest.hexdigest()


def _safe_key(key: str) -> str:
    key = str(key or "")
    path = PurePosixPath(key)
    if (
        not key
        or path.is_absolute()
        or ".." in path.parts
        or len(path.parts) < 2
        or path.parts[0] not in storage.LOCAL_MEDIA_SUBDIRS
    ):
        raise ValueError(f"非法存储 key: {key}")
    return key


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    return info


def _iter_s3_objects():
    client = storage._s3_client()
    token: str | None = None
    while True:
        kwargs = {"Bucket": storage.settings.storage.s3_bucket}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        for item in response.get("Contents") or []:
            key = _safe_key(str(item.get("Key") or ""))
            if key.endswith("/"):
                continue
            yield key, int(item.get("Size") or 0)
        if not response.get("IsTruncated"):
            break
        token = str(response.get("NextContinuationToken") or "")
        if not token:
            raise RuntimeError("S3 分页响应缺少 NextContinuationToken")


def _add_stream(
    bundle: tarfile.TarFile,
    *,
    key: str,
    size: int,
    stream: BinaryIO,
) -> SnapshotObject:
    reader = _HashingReader(stream)
    bundle.addfile(_tar_info(f"{OBJECT_PREFIX}{key}", size), reader)
    if reader.bytes_read != size:
        raise RuntimeError(f"对象长度在快照期间发生变化: {key}")
    return SnapshotObject(key=key, size=size, sha256=reader.hexdigest)


def create_snapshot(output: Path, *, local_root: Path | None = None) -> SnapshotSummary:
    """Atomically create a complete snapshot for the configured backend."""
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.parent / f".{output.name}.{os.getpid()}.tmp"
    if temp.exists():
        raise FileExistsError(temp)
    objects: list[SnapshotObject] = []
    backend = "s3" if storage.is_object_storage_enabled() else "local"
    root = (local_root or storage.ROOT).resolve()
    try:
        with tarfile.open(temp, "w:gz") as bundle:
            if backend == "s3":
                client = storage._s3_client()
                for key, size in _iter_s3_objects():
                    response = client.get_object(
                        Bucket=storage.settings.storage.s3_bucket,
                        Key=key,
                    )
                    body = response["Body"]
                    try:
                        objects.append(_add_stream(bundle, key=key, size=size, stream=body))
                    finally:
                        body.close()
            else:
                if not root.is_dir():
                    raise FileNotFoundError(f"媒体目录不存在: {root}")
                for key, path in iter_local_objects(root):
                    with path.open("rb") as stream:
                        objects.append(
                            _add_stream(
                                bundle,
                                key=_safe_key(key),
                                size=path.stat().st_size,
                                stream=stream,
                            )
                        )
            manifest = json.dumps(
                {
                    "format": SNAPSHOT_FORMAT,
                    "version": SNAPSHOT_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "source_backend": backend,
                    "objects": [item.__dict__ for item in objects],
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            bundle.addfile(_tar_info(MANIFEST_NAME, len(manifest)), io.BytesIO(manifest))
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return SnapshotSummary(
        objects=len(objects),
        bytes=sum(item.size for item in objects),
    )


def _read_manifest(bundle: tarfile.TarFile) -> tuple[dict, dict[str, tarfile.TarInfo]]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in bundle.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not member.isfile():
            raise ValueError(f"不安全的媒体快照成员: {member.name}")
        if member.name in members:
            raise ValueError(f"媒体快照包含重复成员: {member.name}")
        members[member.name] = member
    manifest_member = members.get(MANIFEST_NAME)
    if manifest_member is None or manifest_member.size > MAX_MANIFEST_BYTES:
        raise ValueError("媒体快照缺少有效清单")
    manifest_file = bundle.extractfile(manifest_member)
    if manifest_file is None:
        raise ValueError("无法读取媒体快照清单")
    manifest = json.load(manifest_file)
    if not isinstance(manifest, dict):
        raise ValueError("媒体快照清单格式无效")
    if manifest.get("format") != SNAPSHOT_FORMAT or manifest.get("version") != SNAPSHOT_VERSION:
        raise ValueError("不支持的媒体快照格式")
    return manifest, members


def _manifest_objects(manifest: dict) -> list[SnapshotObject]:
    raw_objects = manifest.get("objects")
    if not isinstance(raw_objects, list):
        raise ValueError("媒体快照清单对象无效")
    objects: list[SnapshotObject] = []
    seen: set[str] = set()
    for raw in raw_objects:
        if not isinstance(raw, dict):
            raise ValueError("媒体快照清单对象无效")
        key = _safe_key(str(raw.get("key") or ""))
        if key in seen:
            raise ValueError(f"媒体快照清单包含重复 key: {key}")
        seen.add(key)
        try:
            size = int(raw["size"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"媒体快照对象容量无效: {key}") from error
        sha256 = str(raw.get("sha256") or "")
        if size < 0 or len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError(f"媒体快照对象校验信息无效: {key}")
        objects.append(SnapshotObject(key=key, size=size, sha256=sha256))
    return objects


def _verify_open_snapshot(
    bundle: tarfile.TarFile,
) -> tuple[list[SnapshotObject], dict[str, tarfile.TarInfo]]:
    manifest, members = _read_manifest(bundle)
    objects = _manifest_objects(manifest)
    expected_members = {MANIFEST_NAME, *(f"{OBJECT_PREFIX}{item.key}" for item in objects)}
    if set(members) != expected_members:
        raise ValueError("媒体快照成员与清单不一致")
    for item in objects:
        member = members[f"{OBJECT_PREFIX}{item.key}"]
        if member.size != item.size:
            raise ValueError(f"媒体快照对象容量不一致: {item.key}")
        stream = bundle.extractfile(member)
        if stream is None:
            raise ValueError(f"无法读取媒体快照对象: {item.key}")
        reader = _HashingReader(stream)
        while reader.read(1024 * 1024):
            pass
        if reader.bytes_read != item.size or reader.hexdigest != item.sha256:
            raise ValueError(f"媒体快照对象 SHA-256 不一致: {item.key}")
    return objects, members


def verify_snapshot(archive: Path) -> SnapshotSummary:
    with tarfile.open(archive, "r:gz") as bundle:
        objects, _ = _verify_open_snapshot(bundle)
    return SnapshotSummary(objects=len(objects), bytes=sum(item.size for item in objects))


def restore_snapshot(
    archive: Path,
    *,
    execute: bool = False,
    overwrite: bool = False,
) -> SnapshotSummary:
    """Verify a snapshot and optionally restore it to the configured backend."""
    with tarfile.open(archive, "r:gz") as bundle:
        objects, members = _verify_open_snapshot(bundle)
        summary = SnapshotSummary(objects=len(objects), bytes=sum(item.size for item in objects))
        if not execute:
            return summary
        current_hashes = {item.key: storage.object_sha256(item.key) for item in objects}
        conflicts = [
            item.key
            for item in objects
            if current_hashes[item.key] is not None
            and current_hashes[item.key] != item.sha256
        ]
        if conflicts and not overwrite:
            detail = ", ".join(conflicts[:5])
            if len(conflicts) > 5:
                detail += f" 等 {len(conflicts)} 个对象"
            raise RuntimeError(f"目标对象已存在且内容不同: {detail}")
        for item in objects:
            current_hash = current_hashes[item.key]
            if current_hash == item.sha256:
                summary.skipped += 1
                continue
            source = bundle.extractfile(members[f"{OBJECT_PREFIX}{item.key}"])
            if source is None:
                raise RuntimeError(f"无法读取媒体快照对象: {item.key}")
            suffix = Path(item.key).suffix
            fd, temp_name = tempfile.mkstemp(prefix="media-restore-", suffix=suffix)
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                with temp_path.open("wb") as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
                storage.upload_existing(temp_path, item.key, overwrite=overwrite)
            finally:
                temp_path.unlink(missing_ok=True)
            if storage.object_sha256(item.key) != item.sha256:
                raise RuntimeError(f"媒体快照恢复后校验失败: {item.key}")
            summary.restored += 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Create, verify, or restore media snapshots")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="Create a complete media snapshot")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--local-root", type=Path)
    verify = subparsers.add_parser("verify", help="Verify manifest, members, size, and SHA-256")
    verify.add_argument("--archive", type=Path, required=True)
    restore = subparsers.add_parser("restore", help="Restore to the configured storage backend")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--execute", action="store_true", help="Perform writes; default is dry-run")
    restore.add_argument("--overwrite", action="store_true", help="Replace existing mismatched objects")
    args = parser.parse_args()

    if args.command == "create":
        summary = create_snapshot(args.output, local_root=args.local_root)
    elif args.command == "verify":
        summary = verify_snapshot(args.archive)
    else:
        if args.overwrite and not args.execute:
            parser.error("--overwrite 必须与 --execute 同时使用")
        summary = restore_snapshot(
            args.archive,
            execute=bool(args.execute),
            overwrite=bool(args.overwrite),
        )
    print(
        f"summary: objects={summary.objects} bytes={summary.bytes} "
        f"restored={summary.restored} skipped={summary.skipped}"
    )


if __name__ == "__main__":
    main()
