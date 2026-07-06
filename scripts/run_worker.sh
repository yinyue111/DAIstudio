#!/usr/bin/env bash
# Celery worker (generation tasks). Defaults to a real process pool so image
# tasks do not queue behind one another. Set WORKER_POOL=solo locally only when
# debugging a macOS fork-safety issue.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
source .venv/bin/activate
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
ROLE="${WORKER_ROLE:-all}"
case "$ROLE" in
  critical)
    DEFAULT_QUEUES="default,payment,video_poll,cleanup"
    DEFAULT_CONCURRENCY=2
    ;;
  image)
    DEFAULT_QUEUES="image"
    DEFAULT_CONCURRENCY=4
    ;;
  video)
    DEFAULT_QUEUES="video_submit,video_download"
    DEFAULT_CONCURRENCY=1
    ;;
  video-submit)
    DEFAULT_QUEUES="video_submit"
    DEFAULT_CONCURRENCY=1
    ;;
  video-download)
    DEFAULT_QUEUES="video_download"
    DEFAULT_CONCURRENCY=1
    ;;
  parse)
    DEFAULT_QUEUES="parse"
    DEFAULT_CONCURRENCY=1
    ;;
  all)
    DEFAULT_QUEUES="default,image,video_submit,video_poll,video_download,parse,cleanup,payment"
    DEFAULT_CONCURRENCY=4
    ;;
  *)
    echo "unknown WORKER_ROLE=$ROLE; expected critical/image/video/video-submit/video-download/parse/all" >&2
    exit 2
    ;;
esac
QUEUES="${WORKER_QUEUES:-$DEFAULT_QUEUES}"
POOL="${WORKER_POOL:-prefork}"
CONCURRENCY="${WORKER_CONCURRENCY:-$DEFAULT_CONCURRENCY}"
NODE_NAME="${WORKER_NAME:-${ROLE}@%h}"
CMD=(celery -A app.celery_app.celery_app worker -l info -n "$NODE_NAME" --pool="$POOL" --concurrency="$CONCURRENCY" -Q "$QUEUES")
if [[ "${1:-}" == "--print-command" ]]; then
  printf '%q ' "${CMD[@]}"
  printf '\n'
  exit 0
fi
exec "${CMD[@]}"
