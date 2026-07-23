#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cmd="$("$ROOT/scripts/run_worker.sh" --print-command)"

case "$cmd" in
  *"--pool=threads"*"--concurrency=4"*) ;;
  *)
    echo "expected default worker command to use threads concurrency=4, got: $cmd" >&2
    exit 1
    ;;
esac

solo_cmd="$(WORKER_POOL=solo WORKER_CONCURRENCY=1 "$ROOT/scripts/run_worker.sh" --print-command)"
case "$solo_cmd" in
  *"--pool=solo"*"--concurrency=1"*) ;;
  *)
    echo "expected explicit solo worker command to be preserved, got: $solo_cmd" >&2
    exit 1
    ;;
esac

echo "worker parallelism script test passed"

critical_cmd="$(WORKER_ROLE=critical "$ROOT/scripts/run_worker.sh" --print-command)"
case "$critical_cmd" in
  *"--pool=threads"*"--concurrency=2"*"default\\,payment\\,video_poll\\,cleanup"*) ;;
  *)
    echo "expected critical worker role to use threads concurrency=2 and isolate short queues, got: $critical_cmd" >&2
    exit 1
    ;;
esac

parse_cmd="$(WORKER_ROLE=parse "$ROOT/scripts/run_worker.sh" --print-command)"
case "$parse_cmd" in
  *"--pool=solo"*"--concurrency=1"*" -Q parse"*) ;;
  *)
    echo "expected parse worker role to use solo pool and isolate parse queue, got: $parse_cmd" >&2
    exit 1
    ;;
esac

reverse_cmd="$(WORKER_ROLE=reverse "$ROOT/scripts/run_worker.sh" --print-command)"
case "$reverse_cmd" in
  *"--pool=threads"*"--concurrency=2"*" -Q reverse"*) ;;
  *)
    echo "expected reverse worker role to use threads concurrency=2 and isolate reverse queue, got: $reverse_cmd" >&2
    exit 1
    ;;
esac

workflow_cmd="$(WORKER_ROLE=workflow "$ROOT/scripts/run_worker.sh" --print-command)"
case "$workflow_cmd" in
  *"--pool=threads"*"--concurrency=2"*" -Q workflow"*) ;;
  *)
    echo "expected workflow worker role to use threads concurrency=2 and isolate workflow queue, got: $workflow_cmd" >&2
    exit 1
    ;;
esac

if WORKER_ROLE=reverse WORKER_POOL=solo "$ROOT/scripts/run_worker.sh" --print-command >/dev/null 2>&1; then
  echo "reverse worker must reject process pools that can hard-kill active tasks" >&2
  exit 1
fi

video_submit_cmd="$(WORKER_ROLE=video-submit "$ROOT/scripts/run_worker.sh" --print-command)"
case "$video_submit_cmd" in
  *"--pool=solo"*"--concurrency=1"*" -Q video_submit"*) ;;
  *)
    echo "expected video-submit worker role to use solo pool and isolate video_submit queue, got: $video_submit_cmd" >&2
    exit 1
    ;;
esac

video_download_cmd="$(WORKER_ROLE=video-download "$ROOT/scripts/run_worker.sh" --print-command)"
case "$video_download_cmd" in
  *"--pool=solo"*"--concurrency=1"*" -Q video_download"*) ;;
  *)
    echo "expected video-download worker role to use solo pool and isolate video_download queue, got: $video_download_cmd" >&2
    exit 1
    ;;
esac

for service in worker_image worker_video worker_video_download worker_parse worker_reverse worker_workflow; do
  if ! grep -q "^  $service:" "$ROOT/docker-compose.yml"; then
    echo "docker-compose should define isolated $service service" >&2
    exit 1
  fi
done

if ! grep -q 'WORKER_CRITICAL_QUEUES:-default,payment,video_poll,cleanup' "$ROOT/docker-compose.yml"; then
  echo "docker-compose worker should default to critical short queues" >&2
  exit 1
fi

if ! grep -q 'WORKER_CRITICAL_POOL:-threads' "$ROOT/docker-compose.yml"; then
  echo "docker-compose critical worker should default to threads pool" >&2
  exit 1
fi

if ! grep -q 'WORKER_IMAGE_POOL:-threads' "$ROOT/docker-compose.yml"; then
  echo "docker-compose image worker should default to threads pool" >&2
  exit 1
fi

if ! grep -q 'WORKER_VIDEO_SUBMIT_QUEUES:-video_submit' "$ROOT/docker-compose.yml"; then
  echo "docker-compose should isolate video_submit queue" >&2
  exit 1
fi

if ! grep -q 'WORKER_VIDEO_SUBMIT_POOL:-solo' "$ROOT/docker-compose.yml"; then
  echo "docker-compose video-submit worker should default to solo pool" >&2
  exit 1
fi

if ! grep -q 'WORKER_VIDEO_DOWNLOAD_QUEUES:-video_download' "$ROOT/docker-compose.yml"; then
  echo "docker-compose should isolate video_download queue" >&2
  exit 1
fi

if ! grep -q 'WORKER_VIDEO_DOWNLOAD_POOL:-solo' "$ROOT/docker-compose.yml"; then
  echo "docker-compose video-download worker should default to solo pool" >&2
  exit 1
fi

if ! grep -q 'WORKER_PARSE_POOL:-solo' "$ROOT/docker-compose.yml"; then
  echo "docker-compose parse worker should default to solo pool" >&2
  exit 1
fi

if ! grep -q 'WORKER_REVERSE_QUEUES:-reverse' "$ROOT/docker-compose.yml"; then
  echo "docker-compose should isolate reverse queue" >&2
  exit 1
fi

if ! grep -q 'worker -l info --pool=threads --concurrency=${WORKER_REVERSE_CONCURRENCY:-2}' "$ROOT/docker-compose.yml"; then
  echo "docker-compose reverse worker should enforce the threads pool" >&2
  exit 1
fi

if ! grep -q 'WORKER_REVERSE_CONCURRENCY:-2' "$ROOT/docker-compose.yml"; then
  echo "docker-compose reverse worker should default to concurrency=2" >&2
  exit 1
fi

if ! grep -q 'WORKER_WORKFLOW_QUEUES:-workflow' "$ROOT/docker-compose.yml"; then
  echo "docker-compose should isolate workflow queue" >&2
  exit 1
fi

if ! grep -q 'WORKER_WORKFLOW_POOL:-threads' "$ROOT/docker-compose.yml"; then
  echo "docker-compose workflow worker should default to threads pool" >&2
  exit 1
fi

if ! grep -q 'WORKER_WORKFLOW_CONCURRENCY:-2' "$ROOT/docker-compose.yml"; then
  echo "docker-compose workflow worker should default to concurrency=2" >&2
  exit 1
fi

echo "docker-compose worker isolation test passed"
