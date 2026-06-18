#!/usr/bin/env bash
# Celery beat scheduler. Run exactly one instance for cleanup/resume jobs.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
source .venv/bin/activate
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
celery -A app.celery_app.celery_app beat -l info
