#!/usr/bin/env bash
# Pod entrypoint: boot -> supercronic (background) -> server + Temporal worker (one process, foreground).
set -euo pipefail

ladderframe boot

if compgen -G "${LADDERFRAME_ROOT}/etc/cron.d/*" >/dev/null || compgen -G "${LADDERFRAME_SHARE:-/nonexistent}/etc/cron.d/*" >/dev/null; then
  ladderframe cron &
  CRON_PID=$!
  trap 'kill "${CRON_PID}" 2>/dev/null || true' EXIT
fi

# Phase 2: `serve` starts FastAPI and the Temporal worker in the same process.
exec ladderframe "${@:-serve}"
