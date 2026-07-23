#!/bin/bash
# PostgreSQL + local media backup. Run inside a maintenance window: the
# sequential database and media snapshots are not transactionally consistent.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
[ -n "$PYTHON_BIN" ] || { echo "[backup] Python runtime not found" >&2; exit 1; }

read_env_value() {
  local key="$1" file="$ROOT/.env"
  [ -f "$file" ] || return 1
  awk -v key="$key" '
    /^[[:space:]]*(#|$)/ { next }
    {
      line=$0; sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      eq=index(line, "="); if (eq == 0) next
      k=substr(line, 1, eq - 1); v=substr(line, eq + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
      if (k != key) next
      sub(/^[[:space:]]+/, "", v); sub(/[[:space:]]+$/, "", v)
      if ((substr(v,1,1) == "\"" && substr(v,length(v),1) == "\"") ||
          (substr(v,1,1) == "'" && substr(v,length(v),1) == "'"))
        v=substr(v, 2, length(v) - 2)
      print v; found=1; exit
    }
    END { if (!found) exit 1 }
  ' "$file"
}

write_connection_files() {
  DATABASE_URL_VALUE="$1" PGPASSWORD_VALUE="$2" PGPASS_PATH="$3" PARSED_PATH="$4" "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

url = os.environ["DATABASE_URL_VALUE"].replace("postgresql+psycopg2://", "postgresql://", 1)
parts = urlsplit(url)
if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
    raise SystemExit("DATABASE_URL must be a PostgreSQL URL with a host")
values = [parts.hostname, str(parts.port or 5432), unquote(parts.username or ""), unquote(parts.path.lstrip("/"))]
if not values[2] or not values[3] or any("\n" in value or "\r" in value for value in values):
    raise SystemExit("DATABASE_URL must include a valid username and database name")
url_password = unquote(parts.password) if parts.password is not None else None
env_password = os.environ.get("PGPASSWORD_VALUE") or None
if url_password is not None and env_password is not None and url_password != env_password:
    raise SystemExit("DATABASE_URL password and PGPASSWORD disagree")
password = url_password if url_password is not None else (env_password or "")
if "\n" in password or "\r" in password:
    raise SystemExit("DATABASE_URL password contains an invalid newline")
escape = lambda value: value.replace("\\", "\\\\").replace(":", "\\:")
Path(os.environ["PGPASS_PATH"]).write_text(
    ":".join(escape(value) for value in [values[0], values[1], values[3], values[2], password]) + "\n",
    encoding="utf-8",
)
Path(os.environ["PARSED_PATH"]).write_text("\n".join(values) + "\n", encoding="utf-8")
PY
  chmod 600 "$3"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

DATABASE_URL="${DATABASE_URL:-$(read_env_value DATABASE_URL || true)}"
BACKUP_DIR="${BACKUP_DIR:-$(read_env_value BACKUP_DIR || true)}"
MEDIA_DIR="${MEDIA_DIR:-$(read_env_value MEDIA_DIR || true)}"
PGPASSWORD="${PGPASSWORD:-$(read_env_value PGPASSWORD || true)}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-$(read_env_value BACKUP_RETENTION_DAYS || true)}"
: "${DATABASE_URL:?DATABASE_URL not set}"

OUT_DIR="${BACKUP_DIR:-$ROOT/backups}"
MEDIA_DIR="${MEDIA_DIR:-$ROOT/storage}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
case "$BACKUP_RETENTION_DAYS" in
  ''|*[!0-9]*) echo "[backup] BACKUP_RETENTION_DAYS must be a non-negative integer" >&2; exit 1 ;;
esac
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

STAMP="${BACKUP_STAMP:-$(date +%Y%m%d_%H%M%S)}"
FINAL="$OUT_DIR/ai_studio_$STAMP"
LOCK="$OUT_DIR/.ai_studio_${STAMP}.publish.lock"
mkdir "$LOCK" 2>/dev/null || { echo "[backup] another backup owns stamp $STAMP" >&2; exit 1; }
TMP="" PGPASS="" PARSED=""
cleanup() { [ -z "$TMP" ] || rm -rf "$TMP"; rm -f "$PGPASS" "$PARSED"; rmdir "$LOCK" 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
[ ! -e "$FINAL" ] || { echo "[backup] destination already exists: $FINAL" >&2; exit 1; }
TMP="$(mktemp -d "$OUT_DIR/.ai_studio_${STAMP}.tmp.XXXXXX")"
PGPASS="$(mktemp "$OUT_DIR/.pgpass.XXXXXX")"
PARSED="$(mktemp "$OUT_DIR/.connection.XXXXXX")"

write_connection_files "$DATABASE_URL" "$PGPASSWORD" "$PGPASS" "$PARSED"
unset PGPASSWORD
{
  IFS= read -r PGHOST
  IFS= read -r PGPORT
  IFS= read -r PGUSER
  IFS= read -r PGDATABASE
} < "$PARSED"
export PGPASSFILE="$PGPASS"

echo "[backup] creating database snapshot"
pg_dump --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" --dbname="$PGDATABASE" \
  --no-password | gzip > "$TMP/database.sql.gz"
gzip -t "$TMP/database.sql.gz"

echo "[backup] creating media snapshot"
(
  cd "$ROOT"
  "$PYTHON_BIN" -m scripts.media_snapshot create \
    --output "$TMP/media.tar.gz" \
    --local-root "$MEDIA_DIR"
  "$PYTHON_BIN" -m scripts.media_snapshot verify --archive "$TMP/media.tar.gz"
)

{
  printf '%s  %s\n' "$(sha256_file "$TMP/database.sql.gz")" "database.sql.gz"
  printf '%s  %s\n' "$(sha256_file "$TMP/media.tar.gz")" "media.tar.gz"
} > "$TMP/SHA256SUMS"
cat > "$TMP/BACKUP_INFO.txt" <<EOF
Created: $STAMP
Database: $PGHOST:$PGPORT/$PGDATABASE
Media source: configured STORAGE_BACKEND (local root override: $MEDIA_DIR)

This database and media backup is sequential and is not transactionally consistent.
Create it during a maintenance window with application writes stopped.
SHA256SUMS detects accidental corruption; it is not a cryptographic signature or proof of authenticity.
Validate restoration with scripts/verify_restore.sh against an explicit non-production temporary database.
EOF

mv "$TMP" "$FINAL"
TMP=""
find "$OUT_DIR" -maxdepth 1 -type d -name 'ai_studio_*' \
  -mtime "+$BACKUP_RETENTION_DAYS" -exec rm -rf {} + 2>/dev/null || true
echo "[backup] published $FINAL"
