#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cmd="$("$ROOT/scripts/run_worker.sh" --print-command)"

case "$cmd" in
  *"--pool=prefork"*"--concurrency=4"*) ;;
  *)
    echo "expected default worker command to use prefork concurrency=4, got: $cmd" >&2
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
  *"--concurrency=2"*"default\\,payment\\,video_poll\\,cleanup"*) ;;
  *)
    echo "expected critical worker role to isolate short queues, got: $critical_cmd" >&2
    exit 1
    ;;
esac

parse_cmd="$(WORKER_ROLE=parse "$ROOT/scripts/run_worker.sh" --print-command)"
case "$parse_cmd" in
  *"--concurrency=1"*" -Q parse"*) ;;
  *)
    echo "expected parse worker role to isolate parse queue, got: $parse_cmd" >&2
    exit 1
    ;;
esac

video_submit_cmd="$(WORKER_ROLE=video-submit "$ROOT/scripts/run_worker.sh" --print-command)"
case "$video_submit_cmd" in
  *"--concurrency=1"*" -Q video_submit"*) ;;
  *)
    echo "expected video-submit worker role to isolate video_submit queue, got: $video_submit_cmd" >&2
    exit 1
    ;;
esac

video_download_cmd="$(WORKER_ROLE=video-download "$ROOT/scripts/run_worker.sh" --print-command)"
case "$video_download_cmd" in
  *"--concurrency=1"*" -Q video_download"*) ;;
  *)
    echo "expected video-download worker role to isolate video_download queue, got: $video_download_cmd" >&2
    exit 1
    ;;
esac

for service in worker_image worker_video worker_video_download worker_parse; do
  if ! grep -q "^  $service:" "$ROOT/docker-compose.yml"; then
    echo "docker-compose should define isolated $service service" >&2
    exit 1
  fi
done

if ! grep -q 'WORKER_CRITICAL_QUEUES:-default,payment,video_poll,cleanup' "$ROOT/docker-compose.yml"; then
  echo "docker-compose worker should default to critical short queues" >&2
  exit 1
fi

if ! grep -q 'WORKER_VIDEO_SUBMIT_QUEUES:-video_submit' "$ROOT/docker-compose.yml"; then
  echo "docker-compose should isolate video_submit queue" >&2
  exit 1
fi

if ! grep -q 'WORKER_VIDEO_DOWNLOAD_QUEUES:-video_download' "$ROOT/docker-compose.yml"; then
  echo "docker-compose should isolate video_download queue" >&2
  exit 1
fi

echo "docker-compose worker isolation test passed"
