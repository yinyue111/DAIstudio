"""Migrate stable local media keys to the configured S3/MinIO bucket.

The command is dry-run by default and never deletes local files, which keeps
rollback as simple as switching ``STORAGE_BACKEND`` back to ``local``.

Usage from ``backend/``::

    python -m scripts.migrate_media_storage
    python -m scripts.migrate_media_storage --execute --verify sha256
    python -m scripts.migrate_media_storage --verify-only --verify sha256
    python -m scripts.migrate_media_storage --configure-lifecycle --execute
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.services import storage


@dataclass
class MigrationSummary:
    discovered: int = 0
    uploaded: int = 0
    skipped: int = 0
    verified: int = 0
    failed: int = 0


def iter_local_objects(root: Path):
    for subdir in sorted(storage.LOCAL_MEDIA_SUBDIRS):
        directory = root / subdir
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if (
                path.is_file()
                and not path.is_symlink()
                and not path.name.startswith((".storage-", ".download-"))
            ):
                yield path.relative_to(root).as_posix(), path


def local_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_object(key: str, path: Path, mode: str) -> tuple[bool, str]:
    remote_size = storage.remote_object_size(key)
    local_size = path.stat().st_size
    if remote_size != local_size:
        return False, f"size local={local_size} remote={remote_size}"
    if mode == "sha256":
        remote_hash = storage.object_sha256(key)
        local_hash = local_sha256(path)
        if remote_hash != local_hash:
            return False, f"sha256 local={local_hash} remote={remote_hash}"
    return True, "ok"


def migrate(*, execute: bool, verify_only: bool, verify: str, overwrite: bool) -> MigrationSummary:
    if not storage.is_object_storage_enabled():
        raise RuntimeError("STORAGE_BACKEND 必须设置为 s3")
    summary = MigrationSummary()
    for key, path in iter_local_objects(storage.ROOT):
        summary.discovered += 1
        try:
            if not verify_only:
                if not execute:
                    print(f"[dry-run] upload {key} ({path.stat().st_size} bytes)")
                    continue
                uploaded = storage.upload_existing(path, key, overwrite=overwrite)
                summary.uploaded += int(uploaded)
                summary.skipped += int(not uploaded)
            if execute or verify_only:
                valid, detail = verify_object(key, path, verify)
                if not valid:
                    raise RuntimeError(detail)
                summary.verified += 1
                print(f"[ok] {key}")
        except Exception as error:  # noqa: BLE001 - continue and report the complete migration set
            summary.failed += 1
            print(f"[failed] {key}: {error}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate local media to S3/MinIO")
    parser.add_argument("--execute", action="store_true", help="Perform writes; default is dry-run")
    parser.add_argument("--verify-only", action="store_true", help="Only verify objects already uploaded")
    parser.add_argument("--verify", choices=("size", "sha256"), default="sha256")
    parser.add_argument("--overwrite", action="store_true", help="Replace remote objects with local bytes")
    parser.add_argument(
        "--configure-lifecycle",
        action="store_true",
        help="Install the non-destructive incomplete-upload lifecycle rule",
    )
    parser.add_argument("--abort-incomplete-days", type=int, default=7)
    args = parser.parse_args()
    if args.verify_only and args.overwrite:
        parser.error("--verify-only 不能与 --overwrite 同时使用")
    if args.configure_lifecycle and not args.execute:
        parser.error("配置生命周期规则必须显式传入 --execute")

    summary = migrate(
        execute=bool(args.execute),
        verify_only=bool(args.verify_only),
        verify=args.verify,
        overwrite=bool(args.overwrite),
    )
    if args.configure_lifecycle:
        storage.configure_lifecycle(abort_incomplete_days=args.abort_incomplete_days)
        print("[ok] lifecycle configured")
    print(
        "summary: "
        f"discovered={summary.discovered} uploaded={summary.uploaded} "
        f"skipped={summary.skipped} verified={summary.verified} failed={summary.failed}"
    )
    if summary.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
