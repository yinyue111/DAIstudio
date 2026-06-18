#!/usr/bin/env bash
# PostgreSQL backup via pg_dump. Reads DATABASE_URL from backend/.env (or env).
# Schedule daily, e.g. crontab:
#   0 2 * * * cd /path/to/backend && ./scripts/backup_db.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a

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
