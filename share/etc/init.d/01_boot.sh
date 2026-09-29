#!/usr/bin/env bash
# Base system bootstrap: installs jq, mq, rg, gh and supercronic if missing.
# Idempotent — every tool is checked with `command -v` first, so re-running is safe.
# apt packages come from signed repositories. Every downloaded binary is checked against a SHA-256:
# pinned below for the default versions (set <TOOL>_SHA256 when you override a version), or the
# digest GitHub publishes for the asset in the latest-release fallbacks used on hosts without apt.
# Root replaces this file by shipping its own etc/init.d/01_boot.sh.
set -euo pipefail

BIN_DIR="${LADDERFRAME_BIN_DIR:-/usr/local/bin}"
SUPERCRONIC_VERSION="${SUPERCRONIC_VERSION:-v0.2.49}"
MQ_VERSION="${MQ_VERSION:-v0.9.2}"
JQ_VERSION="${JQ_VERSION:-jq-1.8.2}"

declare -A PINNED_SHA256=(
  [supercronic-v0.2.49-amd64]=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1
  [supercronic-v0.2.49-arm64]=02aa0cb229ba09050cba6638059dadb9eedc2276632ea43d6a57a2f8c1629dd5
  [mq-v0.9.2-amd64]=993c42b916b3106b92c6318d274b9387107fb1b4f0b6c58981e9f150e2cf5a5f
  [mq-v0.9.2-arm64]=2b9c4e1b18a8fd550db5eaa4352ae510825ae1ff0712477718e9ad3d86b4c42a
  [jq-jq-1.8.2-amd64]=b1c22172dd303f3be49e935aa56aa48a8b7a46e0bc838b4997d3bb451495870f
  [jq-jq-1.8.2-arm64]=8b85c817833814ddca00a144c33705546355afccf0cf39b188f3cdb48b852309
)

log() { echo "[01_boot] $*"; }
have() { command -v "$1" >/dev/null 2>&1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if have sudo; then SUDO="sudo"; else
    BIN_DIR="${HOME}/.local/bin"; mkdir -p "$BIN_DIR"; export PATH="$BIN_DIR:$PATH"
  fi
fi

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64; RUST_ARCH=x86_64 ;;
  aarch64|arm64) ARCH=arm64; RUST_ARCH=aarch64 ;;
  *) echo "unsupported architecture $(uname -m)"; exit 1 ;;
esac

apt_install() {
  if have apt-get && [ -n "$SUDO" -o "$(id -u)" -eq 0 ]; then
    $SUDO apt-get update -qq
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq --no-install-recommends "$@"
    return 0
  fi
  return 1
}

download() { curl -fsSL --retry 3 -o "$2" "$1"; }

# verify <file> <sha256> <name>
verify() {
  if [ -z "$2" ]; then log "ERROR: no SHA-256 known for $3; set its *_SHA256 variable"; return 1; fi
  echo "$2  $1" | sha256sum -c --status - 2>/dev/null || { log "ERROR: SHA-256 mismatch for $3"; return 1; }
}

# pinned <tool> <version>: the SHA-256 for this architecture, from <TOOL>_SHA256 or the table above
pinned() {
  local override="${1^^}_SHA256"
  echo "${!override:-${PINNED_SHA256[$1-$2-$ARCH]:-}}"
}

# latest_asset <owner/repo> <jq filter on the asset name>: "<url> <sha256>" of the latest release's asset
latest_asset() {
  curl -fsSL "https://api.github.com/repos/$1/releases/latest" \
    | jq -r ".assets[] | select(.name | test(\"$2\")) | \"\(.browser_download_url) \((.digest // \"\") | ltrimstr(\"sha256:\"))\"" \
    | head -1
}

install_bin() { $SUDO install -m 0755 "$1" "$BIN_DIR/$2"; }

# curl + ca-certificates are needed for everything below.
have curl || apt_install curl ca-certificates

if ! have jq; then
  log "installing jq"
  apt_install jq || {
    download "https://github.com/jqlang/jq/releases/download/${JQ_VERSION}/jq-linux-${ARCH}" /tmp/jq
    verify /tmp/jq "$(pinned jq "$JQ_VERSION")" jq && install_bin /tmp/jq jq
  }
fi

if ! have rg; then
  log "installing ripgrep"
  apt_install ripgrep || {
    read -r url sha < <(latest_asset BurntSushi/ripgrep "${RUST_ARCH}-unknown-linux-(musl|gnu).tar.gz$")
    download "$url" /tmp/rg.tgz && verify /tmp/rg.tgz "$sha" ripgrep \
      && tar -xzf /tmp/rg.tgz -C /tmp && install_bin /tmp/ripgrep-*/rg rg
  }
fi

if ! have gh; then
  log "installing GitHub CLI"
  if have apt-get && [ -n "$SUDO" -o "$(id -u)" -eq 0 ]; then
    download https://cli.github.com/packages/githubcli-archive-keyring.gpg /tmp/gh.gpg
    $SUDO install -m 0644 /tmp/gh.gpg /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=${ARCH} signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | $SUDO tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    apt_install gh
  else
    read -r url sha < <(latest_asset cli/cli "linux_${ARCH}.tar.gz$")
    download "$url" /tmp/gh.tgz && verify /tmp/gh.tgz "$sha" gh \
      && tar -xzf /tmp/gh.tgz -C /tmp && install_bin /tmp/gh_*_linux_${ARCH}/bin/gh gh
  fi
fi

if ! have mq; then
  # mq: jq-like query tool for Markdown (https://github.com/harehare/mq)
  log "installing mq"
  download "https://github.com/harehare/mq/releases/download/${MQ_VERSION}/mq-${RUST_ARCH}-unknown-linux-gnu" /tmp/mq \
    && verify /tmp/mq "$(pinned mq "$MQ_VERSION")" mq && install_bin /tmp/mq mq \
    || log "WARNING: mq was not installed; check the asset name for your platform and MQ_SHA256"
fi

if ! have supercronic; then
  log "installing supercronic ${SUPERCRONIC_VERSION}"
  download "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${ARCH}" /tmp/supercronic
  verify /tmp/supercronic "$(pinned supercronic "$SUPERCRONIC_VERSION")" supercronic
  install_bin /tmp/supercronic supercronic
fi

log "ready: $(for t in jq rg gh mq supercronic; do have $t && printf '%s ' "$t"; done)"
