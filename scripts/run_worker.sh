#!/usr/bin/env bash
# Celery worker (generation tasks). Defaults to non-forking pools for local and
# bare-metal runs because macOS forked workers can crash in native DB/SSL/image
# libraries after the app has already imported them. Image keeps concurrency via
# the threads pool.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
ROLE="${WORKER_ROLE:-all}"
DEFAULT_POOL="threads"
case "$ROLE" in
  critical)
    DEFAULT_QUEUES="default,payment,video_poll,cleanup"
    DEFAULT_CONCURRENCY=2
    DEFAULT_POOL="threads"
    ;;
  image)
    DEFAULT_QUEUES="image"
    DEFAULT_CONCURRENCY=4
    DEFAULT_POOL="threads"
    ;;
  video)
    DEFAULT_QUEUES="video_submit,video_download"
    DEFAULT_CONCURRENCY=1
    DEFAULT_POOL="solo"
    ;;
  video-submit)
    DEFAULT_QUEUES="video_submit"
    DEFAULT_CONCURRENCY=1
    DEFAULT_POOL="solo"
    ;;
  video-download)
    DEFAULT_QUEUES="video_download"
    DEFAULT_CONCURRENCY=1
    DEFAULT_POOL="solo"
    ;;
  parse)
    DEFAULT_QUEUES="parse"
    DEFAULT_CONCURRENCY=1
    DEFAULT_POOL="solo"
    ;;
  reverse)
    DEFAULT_QUEUES="reverse"
    DEFAULT_CONCURRENCY=2
    DEFAULT_POOL="threads"
    ;;
  workflow)
    DEFAULT_QUEUES="workflow"
    DEFAULT_CONCURRENCY=2
    DEFAULT_POOL="threads"
    ;;
  all)
    DEFAULT_QUEUES="default,image,video_submit,video_poll,video_download,parse,reverse,workflow,cleanup,payment"
    DEFAULT_CONCURRENCY=4
    ;;
  *)
    echo "unknown WORKER_ROLE=$ROLE; expected critical/image/video/video-submit/video-download/parse/reverse/workflow/all" >&2
    exit 2
    ;;
esac
QUEUES="${WORKER_QUEUES:-$DEFAULT_QUEUES}"
POOL="${WORKER_POOL:-$DEFAULT_POOL}"
if [[ "$ROLE" == "reverse" && "$POOL" != "threads" ]]; then
  echo "reverse worker requires WORKER_POOL=threads for cooperative cancellation" >&2
  exit 2
fi
CONCURRENCY="${WORKER_CONCURRENCY:-$DEFAULT_CONCURRENCY}"
NODE_NAME="${WORKER_NAME:-${ROLE}@%h}"
CMD=(celery -A app.celery_app.celery_app worker -l info -n "$NODE_NAME" --pool="$POOL" --concurrency="$CONCURRENCY" -Q "$QUEUES")
if [[ "${1:-}" == "--print-command" ]]; then
  printf '%q ' "${CMD[@]}"
  printf '\n'
  exit 0
fi
source .venv/bin/activate
exec "${CMD[@]}"
