#!/usr/bin/env bash
# Base system bootstrap: installs jq, mq, rg, gh and supercronic if missing.
# Idempotent — every tool is checked with `command -v` first, so re-running is safe.
# Root replaces this file by shipping its own etc/init.d/01_boot.sh.
set -euo pipefail

BIN_DIR="${LADDERFRAME_BIN_DIR:-/usr/local/bin}"
SUPERCRONIC_VERSION="${SUPERCRONIC_VERSION:-v0.2.49}"
MQ_VERSION="${MQ_VERSION:-v0.9.2}"

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

install_bin() { $SUDO install -m 0755 "$1" "$BIN_DIR/$2"; }

# curl + ca-certificates are needed for everything below.
have curl || apt_install curl ca-certificates

if ! have jq; then
  log "installing jq"
  apt_install jq || { download "https://github.com/jqlang/jq/releases/latest/download/jq-linux-${ARCH}" /tmp/jq && install_bin /tmp/jq jq; }
fi

if ! have rg; then
  log "installing ripgrep"
  apt_install ripgrep || {
    url=$(curl -fsSL https://api.github.com/repos/BurntSushi/ripgrep/releases/latest \
      | jq -r ".assets[].browser_download_url | select(test(\"${RUST_ARCH}-unknown-linux-(musl|gnu).tar.gz$\"))" | head -1)
    download "$url" /tmp/rg.tgz && tar -xzf /tmp/rg.tgz -C /tmp && install_bin /tmp/ripgrep-*/rg rg
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
    url=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest \
      | jq -r ".assets[].browser_download_url | select(test(\"linux_${ARCH}.tar.gz$\"))")
    download "$url" /tmp/gh.tgz && tar -xzf /tmp/gh.tgz -C /tmp && install_bin /tmp/gh_*_linux_${ARCH}/bin/gh gh
  fi
fi

if ! have mq; then
  # mq: jq-like query tool for Markdown (https://github.com/harehare/mq)
  log "installing mq"
  if [ "$MQ_VERSION" = latest ]; then
    base="https://github.com/harehare/mq/releases/latest/download"
  else
    base="https://github.com/harehare/mq/releases/download/${MQ_VERSION}"
  fi
  download "${base}/mq-${RUST_ARCH}-unknown-linux-gnu" /tmp/mq && install_bin /tmp/mq mq \
    || log "WARNING: mq download failed; check the asset name for your platform"
fi

if ! have supercronic; then
  log "installing supercronic ${SUPERCRONIC_VERSION}"
  download "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${ARCH}" /tmp/supercronic
  install_bin /tmp/supercronic supercronic
fi

log "ready: $(for t in jq rg gh mq supercronic; do have $t && printf '%s ' "$t"; done)"
