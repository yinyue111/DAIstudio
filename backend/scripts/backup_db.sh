#!/usr/bin/env bash
# PostgreSQL backup via pg_dump. Reads DATABASE_URL from backend/.env (or env).
# Schedule daily, e.g. crontab:
#   0 2 * * * cd /path/to/backend && ./scripts/backup_db.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

read_env_value() {
  local key="$1"
  local file="$ROOT/.env"
  [ -f "$file" ] || return 1
  awk -v key="$key" '
    /^[[:space:]]*(#|$)/ { next }
    {
      line=$0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      eq=index(line, "=")
      if (eq == 0) next
      k=substr(line, 1, eq - 1)
      v=substr(line, eq + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
      if (k != key) next
      sub(/^[[:space:]]+/, "", v)
      sub(/[[:space:]]+$/, "", v)
      if ((substr(v,1,1) == "\"" && substr(v,length(v),1) == "\"") ||
          (substr(v,1,1) == "'"'"'" && substr(v,length(v),1) == "'"'"'")) {
        v=substr(v, 2, length(v) - 2)
      }
      print v
      found=1
      exit
    }
    END { if (!found) exit 1 }
  ' "$file"
}

DATABASE_URL="${DATABASE_URL:-$(read_env_value DATABASE_URL || true)}"
BACKUP_DIR="${BACKUP_DIR:-$(read_env_value BACKUP_DIR || true)}"

: "${DATABASE_URL:?DATABASE_URL not set}"
OUT_DIR="${BACKUP_DIR:-$ROOT/backups}"
mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$OUT_DIR/ai_studio_$STAMP.sql.gz"

# pg_dump understands the SQLAlchemy URL once we strip the +psycopg2 driver tag
PG_URL="${DATABASE_URL/+psycopg2/}"

echo "[backup] dumping to $OUT"
pg_dump "$PG_URL" | gzip > "$OUT"

# keep last 14 days
find "$OUT_DIR" -name 'ai_studio_*.sql.gz' -mtime +14 -delete 2>/dev/null || true
echo "[backup] done"
