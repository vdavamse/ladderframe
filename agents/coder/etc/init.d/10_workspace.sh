#!/usr/bin/env bash
# Agent-specific bootstrap. Runs after share/etc/init.d/01_boot.sh.
set -euo pipefail
mkdir -p "${AGENT_ROOT}/var/log" "${AGENT_ROOT}/var/lib" "${AGENT_ROOT}/tmp"
git config --global --get user.name >/dev/null 2>&1 || git config --global user.name "coder-agent"
git config --global --get user.email >/dev/null 2>&1 || git config --global user.email "coder-agent@localhost"
