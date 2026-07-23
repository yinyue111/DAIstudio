#!/bin/bash
# Verify a bundle against a generated, disposable PostgreSQL database.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
[ -n "$PYTHON_BIN" ] || { echo "[restore] Python runtime not found" >&2; exit 1; }

usage() { echo "Usage: TARGET_DATABASE_URL=... RESTORE_VERIFY_NON_PRODUCTION=1 $0 --bundle DIR" >&2; exit 2; }
BUNDLE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --bundle) [ "$#" -ge 2 ] || usage; BUNDLE="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$BUNDLE" ] || usage
: "${TARGET_DATABASE_URL:?TARGET_DATABASE_URL must provide a password-free admin database connection}"
[ "${RESTORE_VERIFY_NON_PRODUCTION:-}" = 1 ] || { echo "[restore] explicit non-production confirmation is required" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ai-studio-restore.XXXXXX")"
chmod 700 "$WORK"
PGPASS="$WORK/pgpass"
PARSED="$WORK/connection"
TARGET_CREATED=0
cleanup() {
  if [ "$TARGET_CREATED" = 1 ]; then
    PGPASSFILE="$PGPASS" dropdb --if-exists --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" \
      --no-password --maintenance-db="$PGADMIN_DATABASE" "$TEMP_DATABASE" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

TARGET_URL_VALUE="$TARGET_DATABASE_URL" PGPASSWORD_VALUE="${PGPASSWORD:-}" PGPASS_PATH="$PGPASS" PARSED_PATH="$PARSED" "$PYTHON_BIN" - <<'PY'
import os
import re
import secrets
from pathlib import Path
from urllib.parse import unquote, urlsplit

url = os.environ["TARGET_URL_VALUE"].replace("postgresql+psycopg2://", "postgresql://", 1)
parts = urlsplit(url)
host, port = parts.hostname or "", str(parts.port or 5432)
user, admin_db = unquote(parts.username or ""), unquote(parts.path.lstrip("/"))
if parts.scheme not in {"postgres", "postgresql"} or not host or not user or not admin_db:
    raise SystemExit("TARGET_DATABASE_URL must be a complete PostgreSQL admin connection")
if parts.password is not None:
    raise SystemExit("TARGET_DATABASE_URL must not contain a password; use PGPASSWORD")
if re.search(r"(^|[.-])(prod|production)([.-]|$)", host, re.I):
    raise SystemExit("target host must be non-production")
password = os.environ.get("PGPASSWORD_VALUE", "")
if any("\n" in value or "\r" in value for value in [host, port, user, admin_db, password]):
    raise SystemExit("target connection contains an invalid newline")
temp_db = "ai_studio_restore_" + secrets.token_hex(12)
escape = lambda value: value.replace("\\", "\\\\").replace(":", "\\:")
Path(os.environ["PGPASS_PATH"]).write_text(
    ":".join(escape(value) for value in [host, port, "*", user, password]) + "\n", encoding="utf-8"
)
Path(os.environ["PARSED_PATH"]).write_text(
    "\n".join([host, port, user, admin_db, temp_db]) + "\n", encoding="utf-8"
)
PY
chmod 600 "$PGPASS"
unset PGPASSWORD
{
  IFS= read -r PGHOST
  IFS= read -r PGPORT
  IFS= read -r PGUSER
  IFS= read -r PGADMIN_DATABASE
  IFS= read -r TEMP_DATABASE
} < "$PARSED"
export PGPASSFILE="$PGPASS"

for required in database.sql.gz media.tar.gz SHA256SUMS; do
  [ -f "$BUNDLE/$required" ] || { echo "[restore] missing $required" >&2; exit 1; }
done

verify_digest() {
  local expected actual file="$1"
  expected="$(awk -v name="$file" '$2 == name { print $1; found=1; exit } END { if (!found) exit 1 }' "$BUNDLE/SHA256SUMS")" || {
    echo "[restore] checksum entry missing for $file" >&2; return 1;
  }
  if command -v sha256sum >/dev/null 2>&1; then actual="$(sha256sum "$BUNDLE/$file" | awk '{print $1}')";
  else actual="$(shasum -a 256 "$BUNDLE/$file" | awk '{print $1}')"; fi
  [ "$actual" = "$expected" ] || { echo "[restore] checksum mismatch for $file" >&2; return 1; }
}
verify_digest database.sql.gz
verify_digest media.tar.gz
gzip -t "$BUNDLE/database.sql.gz"
if ! (
  cd "$ROOT"
  "$PYTHON_BIN" -m scripts.media_snapshot verify --archive "$BUNDLE/media.tar.gz"
); then
  echo "[restore] unsafe media archive member or invalid media snapshot" >&2
  exit 1
fi

ARCHIVE_PATH="$BUNDLE/media.tar.gz" EXTRACT_PATH="$WORK/media" "$PYTHON_BIN" - <<'PY'
import os
import tarfile
from pathlib import Path, PurePosixPath

archive = Path(os.environ["ARCHIVE_PATH"])
destination = Path(os.environ["EXTRACT_PATH"])
destination.mkdir(mode=0o700)
with tarfile.open(archive, "r:gz") as bundle:
    for member in bundle.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
            raise SystemExit(f"unsafe media archive member: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f"unsupported media archive member: {member.name}")
    bundle.extractall(destination)
PY

DB_ARGS=(--host="$PGHOST" --port="$PGPORT" --username="$PGUSER" --no-password)
createdb "${DB_ARGS[@]}" --maintenance-db="$PGADMIN_DATABASE" "$TEMP_DATABASE"
TARGET_CREATED=1
gzip -dc "$BUNDLE/database.sql.gz" | psql "${DB_ARGS[@]}" --dbname="$TEMP_DATABASE" --set=ON_ERROR_STOP=1 >/dev/null
SCHEMA_PROBE="$(psql "${DB_ARGS[@]}" --dbname="$TEMP_DATABASE" --tuples-only --no-align --set=ON_ERROR_STOP=1 \
  --command="SELECT (
    to_regclass('public.alembic_version') IS NOT NULL
    AND to_regclass('public.users') IS NOT NULL
    AND to_regclass('public.gen_tasks') IS NOT NULL
    AND EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num IS NOT NULL AND btrim(version_num) <> '')
    AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'id')
    AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'gen_tasks' AND column_name = 'id')
  )::text")"
[ "$(printf '%s' "$SCHEMA_PROBE" | tr -d '[:space:]')" = true ] || { echo "[restore] required schema probe failed" >&2; exit 1; }
echo "[restore] corruption checks, media extraction, and temporary database schema probe passed"
