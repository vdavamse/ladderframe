#!/usr/bin/env bash
# Pod entrypoint, run by tini: boot, then the server + Temporal worker (one process) and, when there are
# cron jobs, supercronic beside it. If either one exits, the other is stopped and the container exits
# with that status, so the pod is restarted rather than left serving without cron (or the reverse).
set -euo pipefail

ladderframe boot

# `serve` runs FastAPI and, with runtime.executor: temporal, the Temporal worker in the same process.
if ! compgen -G "${LADDERFRAME_ROOT}/etc/cron.d/*" >/dev/null && ! compgen -G "${LADDERFRAME_SHARE:-/nonexistent}/etc/cron.d/*" >/dev/null; then
  exec ladderframe "${@:-serve}"
fi

ladderframe cron &
cron_pid=$!
ladderframe "${@:-serve}" &
main_pid=$!
# Signal each child once (a second SIGTERM makes uvicorn exit without draining).
stopped=""
stop() {
  if [ -z "${stopped}" ]; then
    stopped=1
    kill -TERM "${cron_pid}" "${main_pid}" 2>/dev/null || true
  fi
}
# On SIGTERM (pod shutdown) pass it on, so running cron jobs and turns can finish.
trap stop TERM INT

status=0
wait -n "${cron_pid}" "${main_pid}" || status=$?
stop
wait "${cron_pid}" "${main_pid}" || true
exit "${status}"
