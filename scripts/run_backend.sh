#!/usr/bin/env bash
# FastAPI API server.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
source .venv/bin/activate
# avoid macOS fork-safety crashes in child processes
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
BIND_HOST="${BIND_HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
if [[ "$BIND_HOST" == "0.0.0.0" || "$BIND_HOST" == "::" ]] && [[ "${ALLOW_DEBUG_BIND_ALL:-}" != "1" ]]; then
  echo "Refusing to bind debug server to $BIND_HOST. Set ALLOW_DEBUG_BIND_ALL=1 explicitly if this is intentional." >&2
  exit 1
fi
uvicorn app.main:app --host "$BIND_HOST" --port "$PORT" --reload
