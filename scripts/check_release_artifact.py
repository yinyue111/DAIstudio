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


def _iter_tree(root: Path):
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        yield rel, path.stat().st_size if path.is_file() else 0


def _iter_archive(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                yield info.filename, int(info.file_size or 0)
        return
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            for member in tf.getmembers():
                yield member.name, int(member.size or 0)
        return
    raise SystemExit(f"unsupported artifact type: {path}")


def _size_violations(entries: list[tuple[str, int]]) -> list[tuple[str, str]]:
    violations: list[tuple[str, str]] = []
    total = sum(size for _name, size in entries)
    public_total = sum(
        size for name, size in entries
        if _normalise(name).as_posix().startswith("frontend/public/")
    )
    if total > MAX_ARTIFACT_BYTES:
        violations.append(("<artifact>", f"artifact size {total} exceeds {MAX_ARTIFACT_BYTES} bytes"))
    if public_total > MAX_FRONTEND_PUBLIC_BYTES:
        violations.append((
            "frontend/public",
            f"frontend public size {public_total} exceeds {MAX_FRONTEND_PUBLIC_BYTES} bytes",
        ))
    for name, size in entries:
        if size > MAX_SINGLE_FILE_BYTES:
            violations.append((name, f"single file size {size} exceeds {MAX_SINGLE_FILE_BYTES} bytes"))
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
    for name, _size in entries:
        reason = _blocked_reason(name)
        if reason:
            violations.append((name, reason))
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
