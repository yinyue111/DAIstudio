#!/usr/bin/env bash
# Celery worker (generation tasks). --pool=solo keeps it simple + macOS-safe.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
source .venv/bin/activate
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
QUEUES="${WORKER_QUEUES:-default,image,video_submit,video_poll,video_download,parse,cleanup,payment}"
celery -A app.celery_app.celery_app worker -l info --pool=solo -Q "$QUEUES"
