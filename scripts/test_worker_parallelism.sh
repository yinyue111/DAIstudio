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

if ! grep -q 'WORKER_CONCURRENCY:-4' "$ROOT/docker-compose.yml"; then
  echo "docker-compose worker should default WORKER_CONCURRENCY to 4" >&2
  exit 1
fi

echo "docker-compose worker parallelism test passed"
