#!/usr/bin/env bash
# Celery worker (generation tasks). Defaults to a real process pool so image
# tasks do not queue behind one another. Set WORKER_POOL=solo locally only when
# debugging a macOS fork-safety issue.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
source .venv/bin/activate
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
QUEUES="${WORKER_QUEUES:-default,image,video_submit,video_poll,video_download,parse,cleanup,payment}"
POOL="${WORKER_POOL:-prefork}"
CONCURRENCY="${WORKER_CONCURRENCY:-4}"
CMD=(celery -A app.celery_app.celery_app worker -l info --pool="$POOL" --concurrency="$CONCURRENCY" -Q "$QUEUES")
if [[ "${1:-}" == "--print-command" ]]; then
  printf '%q ' "${CMD[@]}"
  printf '\n'
  exit 0
fi
exec "${CMD[@]}"
