#!/usr/bin/env bash
# Celery worker (generation tasks). --pool=solo keeps it simple + macOS-safe.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
source .venv/bin/activate
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
celery -A app.celery_app.celery_app worker -l info --pool=solo
