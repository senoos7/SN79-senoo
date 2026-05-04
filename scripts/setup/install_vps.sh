#!/usr/bin/env bash
# ============================================================
# install_vps.sh - One-shot VPS bootstrap for SN-79 (taos.im) miners
#
# Idempotent: safe to re-run. Targets fresh Ubuntu 22.04 / 24.04.
#
# What it installs:
#   - apt build deps + tmux + htop
#   - Node + pm2 (process manager) + pm2-logrotate
#   - uv (fast Python toolchain) -> Python 3.10.9 venv at $REPO/.venv
#   - taos package (pip install -e .)
#   - Example agents copied to ~/.taos/agents
#
# Run from inside the cloned sn-79 repo:
#   ./scripts/setup/install_vps.sh
#
# Override vars (all optional):
#   PYTHON_VERSION=3.10.9   - exact Python version for the venv
#   AGENT_DIR=~/.taos/agents - where example agents are placed
#   SKIP_APT=1               - skip apt commands (use if not root/sudo)
# ============================================================

set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.10.9}"
AGENT_DIR="${AGENT_DIR:-$HOME/.taos/agents}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"

log() { echo -e "\033[1;32m[install_vps]\033[0m $*"; }
warn() { echo -e "\033[1;33m[install_vps]\033[0m $*"; }
fail() { echo -e "\033[1;31m[install_vps]\033[0m $*"; exit 1; }

[ -f "$REPO_ROOT/setup.py" ] && grep -q "^    name=\"taos\"" "$REPO_ROOT/setup.py" \
  || fail "Run this from inside the sn-79 repo (current REPO_ROOT=$REPO_ROOT)"

# ---------- 1. apt build deps ----------
if [ "${SKIP_APT:-0}" != "1" ]; then
  log "Installing apt build dependencies (sudo required)"
  SUDO="sudo"
  if [ "$EUID" -eq 0 ]; then SUDO=""; fi
  $SUDO apt-get update -y
  $SUDO apt-get install -y --no-install-recommends \
    build-essential curl wget git ca-certificates \
    libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
    libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev \
    tmux htop jq
else
  warn "SKIP_APT=1 set, skipping apt step"
fi

# ---------- 2. Node + pm2 ----------
if ! command -v node >/dev/null 2>&1; then
  log "Installing Node.js LTS via NodeSource"
  $SUDO bash -c 'curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -'
  $SUDO apt-get install -y nodejs
else
  log "Node.js present: $(node --version)"
fi

if ! command -v pm2 >/dev/null 2>&1; then
  log "Installing pm2 + pm2-logrotate"
  $SUDO npm install -g pm2
  pm2 install pm2-logrotate || true
  pm2 set pm2-logrotate:max_size 100M || true
  pm2 set pm2-logrotate:compress true || true
  pm2 set pm2-logrotate:retain 14 || true
else
  log "pm2 present: $(pm2 --version)"
fi

# ---------- 3. uv (fast Python manager) ----------
if ! command -v uv >/dev/null 2>&1; then
  log "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin
  export PATH="$HOME/.local/bin:$PATH"
  if ! grep -q '/.local/bin' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  fi
fi
log "uv present: $(uv --version)"

# ---------- 4. Python 3.10.9 venv ----------
if [ ! -d "$VENV_DIR" ]; then
  log "Creating venv with Python $PYTHON_VERSION at $VENV_DIR"
  uv venv --python "$PYTHON_VERSION" "$VENV_DIR"
else
  log "venv already exists at $VENV_DIR"
fi

# Activate venv for the rest of the script
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log "Active python: $(python --version) at $(which python)"

# ---------- 5. Install taos package ----------
# IMPORTANT: `uv venv` does NOT install pip into the venv by default, so
# `python -m pip ...` would fail with "No module named pip". We use `uv pip`
# instead, which is uv's own pip-compatible installer and works without pip
# being present in the venv. It's also significantly faster than pip.
log "Installing TLS libs"
uv pip install -q --upgrade pyopenssl cryptography

log "Installing taos (editable) from $REPO_ROOT"
uv pip install -e "$REPO_ROOT"

# ---------- 6. Verify agents directory in the repo ----------
# The runner script uses $REPO_ROOT/agents directly, so no copy is needed.
# This block is kept as a sanity check + summary for the operator.
if [ -d "$REPO_ROOT/agents" ]; then
  log "Agents available at $REPO_ROOT/agents:"
  ls -1 "$REPO_ROOT/agents" | grep '\.py$' | sed 's/^/  - /'
else
  warn "agents/ directory missing from $REPO_ROOT - the repo may be incomplete."
fi
# Optional: also populate $AGENT_DIR (~/.taos/agents) for backwards compatibility
# with run_miner.sh and any scripts that still expect that path.
if [ "${POPULATE_LEGACY_AGENT_DIR:-1}" = "1" ]; then
  log "Also populating legacy path $AGENT_DIR (set POPULATE_LEGACY_AGENT_DIR=0 to skip)"
  mkdir -p "$AGENT_DIR"
  cp "$REPO_ROOT/agents/"*.py "$AGENT_DIR/" 2>/dev/null || true
fi

# ---------- 7. Quick smoke test ----------
log "Importing taos to verify install"
python -c "import taos, bittensor; print(f'  taos {taos.__version__}, bittensor {bittensor.__version__}')"

# Sanity-check that the custom AdaptiveMarketMakerAgent at least parses + imports.
AGENT_FILE="$REPO_ROOT/agents/AdaptiveMarketMakerAgent.py"
if [ -f "$AGENT_FILE" ]; then
  log "Sanity-checking AdaptiveMarketMakerAgent.py"
  python -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('AdaptiveMarketMakerAgent', '$AGENT_FILE')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert hasattr(mod, 'AdaptiveMarketMakerAgent'), 'class missing'
print('  AdaptiveMarketMakerAgent loads OK')
"
fi

cat <<EOF

============================================================
SN-79 VPS environment ready.

  Repo:        $REPO_ROOT
  venv:        $VENV_DIR
  Agents:      $AGENT_DIR
  Python:      $(python --version)

Next steps:
  1. Create / import your wallet:
       btcli wallet new_coldkey   --wallet.name <coldkey_name>
       btcli wallet new_hotkey    --wallet.name <coldkey_name> --wallet.hotkey <hotkey_name>
  2. Register on SN-79 (or testnet 366 first):
       btcli subnet register --netuid 79  --wallet.name <cold> --wallet.hotkey <hot>
  3. Edit and run the multi-miner script:
       scripts/miner/run/run_multi_miner_sn79.sh
============================================================
EOF
