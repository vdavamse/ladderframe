#!/usr/bin/env bash
# Print a compact summary of the working tree changes.
set -euo pipefail
git diff --stat
