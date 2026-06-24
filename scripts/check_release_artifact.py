#!/usr/bin/env python3
"""Fail release builds that include local secrets, caches, or runtime data.

Run this against a candidate release archive or an unpacked release root, not
the live development workspace. The local workspace intentionally keeps
backend/.venv and frontend/node_modules for development; release artifacts must
not contain them.

Usage:
  scripts/check_release_artifact.py dist/source.zip
  scripts/check_release_artifact.py dist/source.tar.gz
  scripts/check_release_artifact.py /tmp/unpacked-release
"""
from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import NamedTuple


MAX_ARTIFACT_BYTES = 120 * 1024 * 1024
MAX_FRONTEND_PUBLIC_BYTES = 80 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 25 * 1024 * 1024

BLOCKED_DIRS = {
    ".git",
    ".agents",
    ".claude",
    ".codex",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    ".codex-run",
    "__MACOSX",
    "__pycache__",
    "node_modules",
    ".next",
    ".venv",
    "venv",
    "storage",
    "backups",
    "dist",
    "htmlcov",
    "coverage",
    "playwright-report",
    "test-results",
    "logs",
}

BLOCKED_NAMES = {
    ".DS_Store",
    ".coverage",
    "celerybeat-schedule",
    "celerybeat-schedule.db",
}

BLOCKED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".log",
    ".pid",
}


class Entry(NamedTuple):
    name: str
    size: int
    kind: str = "file"
    linkname: str = ""


def _normalise(name: str) -> PurePosixPath:
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".")]
    if parts and parts[0] == "/":
        parts = parts[1:]
    return PurePosixPath(*parts)


def _has_blocked_suffix(path: PurePosixPath) -> str | None:
    suffixes = set(path.suffixes)
    for suffix in sorted(BLOCKED_SUFFIXES):
        if suffix in suffixes:
            return suffix
    return None


def _blocked_reason(name: str) -> str | None:
    raw_path = PurePosixPath(name.replace("\\", "/"))
    if raw_path.is_absolute() or ".." in raw_path.parts:
        return "unsafe archive path"
    path = _normalise(name)
    parts = path.parts
    if not parts:
        return None
    if path.name == ".env.example" or (path.name.startswith(".env.") and path.name.endswith(".example")):
        return None
    if path.name == ".env" or path.name.startswith(".env."):
        return "env file"
    for part in parts:
        if part in BLOCKED_DIRS:
            return f"blocked directory {part}"
    if path.name in BLOCKED_NAMES:
        return f"blocked file {path.name}"
    if path.name.startswith("celerybeat-schedule"):
        return "celery beat runtime schedule"
    if suffix := _has_blocked_suffix(path):
        return f"blocked suffix {suffix}"
    return None


def _blocked_link_reason(linkname: str) -> str | None:
    if not linkname:
        return "empty archive link target"
    link_path = PurePosixPath(linkname.replace("\\", "/"))
    if link_path.is_absolute() or ".." in link_path.parts:
        return "unsafe archive link target"
    return _blocked_reason(linkname)


def _entry_type_reason(entry: Entry) -> str | None:
    if entry.kind == "file" or entry.kind == "dir":
        return None
    if entry.kind in {"symlink", "hardlink"}:
        return _blocked_link_reason(entry.linkname) or f"blocked {entry.kind}"
    return f"blocked special file type {entry.kind}"


def _iter_tree(root: Path):
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            yield Entry(rel, 0, "symlink", path.readlink().as_posix())
        elif path.is_file():
            yield Entry(rel, path.stat().st_size)
        elif path.is_dir():
            yield Entry(rel, 0, "dir")
        else:
            yield Entry(rel, 0, "special")


def _iter_archive(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                mode = (info.external_attr >> 16) & 0o170000
                kind = "dir" if info.is_dir() else "file"
                if mode == 0o120000:
                    kind = "symlink"
                elif mode not in (0, 0o100000, 0o040000):
                    kind = "special"
                yield Entry(info.filename, int(info.file_size or 0), kind)
        return
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            for member in tf.getmembers():
                if member.isdir():
                    kind = "dir"
                elif member.isfile():
                    kind = "file"
                elif member.issym():
                    kind = "symlink"
                elif member.islnk():
                    kind = "hardlink"
                else:
                    kind = member.type.decode("ascii", "ignore") or "special"
                yield Entry(member.name, int(member.size or 0), kind, member.linkname or "")
        return
    raise SystemExit(f"unsupported artifact type: {path}")


def _size_violations(entries: list[Entry]) -> list[tuple[str, str]]:
    violations: list[tuple[str, str]] = []
    total = sum(entry.size for entry in entries)
    public_total = sum(
        entry.size for entry in entries
        if _normalise(entry.name).as_posix().startswith("frontend/public/")
    )
    if total > MAX_ARTIFACT_BYTES:
        violations.append(("<artifact>", f"artifact size {total} exceeds {MAX_ARTIFACT_BYTES} bytes"))
    if public_total > MAX_FRONTEND_PUBLIC_BYTES:
        violations.append((
            "frontend/public",
            f"frontend public size {public_total} exceeds {MAX_FRONTEND_PUBLIC_BYTES} bytes",
        ))
    for entry in entries:
        if entry.size > MAX_SINGLE_FILE_BYTES:
            violations.append((
                entry.name,
                f"single file size {entry.size} exceeds {MAX_SINGLE_FILE_BYTES} bytes",
            ))
    return violations


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    target = Path(argv[1]).resolve()
    if not target.exists():
        print(f"target does not exist: {target}", file=sys.stderr)
        return 2
    entries = list(_iter_tree(target) if target.is_dir() else _iter_archive(target))
    violations: list[tuple[str, str]] = []
    for entry in entries:
        reason = _blocked_reason(entry.name) or _entry_type_reason(entry)
        if reason:
            violations.append((entry.name, reason))
    violations.extend(_size_violations(entries))
    if violations:
        print("release artifact contains blocked local/runtime files:", file=sys.stderr)
        for name, reason in violations[:100]:
            print(f"  {name} ({reason})", file=sys.stderr)
        if len(violations) > 100:
            print(f"  ... {len(violations) - 100} more", file=sys.stderr)
        return 1
    print(f"release artifact check passed: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
