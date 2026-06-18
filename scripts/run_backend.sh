#!/usr/bin/env bash
# FastAPI API server.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
source .venv/bin/activate
# avoid macOS fork-safety crashes in child processes
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
