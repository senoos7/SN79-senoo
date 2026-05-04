#!/usr/bin/env bash
# ============================================================
# run_multi_miner_sn79.sh - manage multiple SN-79 (taos.im) miners via pm2
#
# Each miner entry:  "WALLET  HOTKEY  PORT  PM2_NAME  [AGENT_NAME]"
#   WALLET      - coldkey wallet name; "-" -> DEFAULT_WALLET_NAME
#   HOTKEY      - bittensor hotkey name registered on netuid 79 (or 366 testnet)
#   PORT        - unique axon port per miner (must be open inbound on the VPS)
#   PM2_NAME    - unique pm2 process name
#   AGENT_NAME  - (optional) agent class name in $AGENT_PATH; "-" -> DEFAULT_AGENT_NAME
#                 Per-agent params live in the AGENT_PARAMS associative array below.
#
# Usage:
#   ./scripts/miner/run/run_multi_miner_sn79.sh start
#   ./scripts/miner/run/run_multi_miner_sn79.sh stop
#   ./scripts/miner/run/run_multi_miner_sn79.sh restart
#   ./scripts/miner/run/run_multi_miner_sn79.sh status
#   ./scripts/miner/run/run_multi_miner_sn79.sh logs <pm2_name>
#   ./scripts/miner/run/run_multi_miner_sn79.sh list
# ============================================================

set -e

# ----------------------------------------------------------------
# Shared defaults - used when a miner entry has "-" in that field
# ----------------------------------------------------------------
DEFAULT_WALLET_NAME="${DEFAULT_WALLET_NAME:-superbit-darnsin}"
NETWORK="${NETWORK:-finney}"
NETUID="${NETUID:-79}"

# Resolve repo root from this script's location, then derive paths.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MINER_SCRIPT="$REPO_ROOT/taos/im/neurons/miner.py"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"

# Python interpreter - prefer the repo venv created by install_vps.sh
if [ -x "$VENV_PYTHON" ]; then
  PYTHON="${PYTHON:-$VENV_PYTHON}"
else
  PYTHON="${PYTHON:-$(which python3)}"
fi

# Agent definitions live here (default location used by install_vps.sh).
AGENT_PATH="${AGENT_PATH:-$HOME/.taos/agents}"

# Default agent class to load when a miner entry has no 5th field or "-".
# AdaptiveMarketMakerAgent -> custom inventory-managed MM with stop-loss (this repo)
# RandomMakerAgent         -> safest baseline, places limit orders both sides (no custom logic)
# SimpleRegressorAgent     -> documented default in run_miner.sh, sklearn online regressor
# ImbalanceAgent           -> uses L3 events, more involved
DEFAULT_AGENT_NAME="${DEFAULT_AGENT_NAME:-AdaptiveMarketMakerAgent}"

# Subtensor chain endpoint - explicit URL avoids DNS flapping / WS keepalive timeouts.
# Override: SUBTENSOR_ENDPOINT=wss://... ./run_multi_miner_sn79.sh start
SUBTENSOR_ENDPOINT="${SUBTENSOR_ENDPOINT:-wss://entrypoint-finney.opentensor.ai:443}"

# Public IP of this VPS - REQUIRED when the NIC shows a private IP (10.x / 172.x / 192.168.x).
# Without this, the miner registers a private IP on-chain and validators cannot reach it -> 0 queries.
# Override: AXON_EXTERNAL_IP=1.2.3.4 ./run_multi_miner_sn79.sh start
AXON_EXTERNAL_IP="${AXON_EXTERNAL_IP:-}"

# Wallet directory (matches bittensor default).
WALLET_PATH="${WALLET_PATH:-$HOME/.bittensor/wallets/}"

# Logging level: error|warning|info|debug|trace (info is fine for production)
LOG_LEVEL="${LOG_LEVEL:-info}"

# ----------------------------------------------------------------
# Per-agent parameters
# Keyed by AGENT_NAME. Values are passed through to `--agent.params`
# in the form `key1=val1 key2=val2 ...` (bittensor parses with nargs='*').
# ----------------------------------------------------------------
declare -A AGENT_PARAMS=(
  # Custom competitive agent (this repo). See agents/AdaptiveMarketMakerAgent.py docstring
  # for what every knob does. Defaults below are conservative; tune after observing fills.
  [AdaptiveMarketMakerAgent]="base_qty=0.5 base_half_spread=0.0008 gamma=20.0 vol_window=64 inventory_cap_pct=0.10 stop_loss_pct=0.005 vol_pause_pct=0.70 expiry_seconds=4 requote_tolerance_pct=0.0003 capital_turnover_cap=1.0"
  # Sample agents shipped with the repo
  [SimpleRegressorAgent]="min_quantity=0.1 max_quantity=1.0 expiry_period=200 model=PassiveAggressiveRegressor signal_threshold=0.0025"
  [RandomMakerAgent]="min_quantity=0.1 max_quantity=1.0 expiry_period=200"
  [RandomTakerAgent]="min_quantity=0.1 max_quantity=1.0"
  [ImbalanceAgent]="min_quantity=0.1 max_quantity=1.0 expiry_period=200"
  [MovingHurstAgent]=""
  [OrderOptionAgent]=""
  [FuturesAgent]=""
)

# ----------------------------------------------------------------
# Miner definitions
# Format: "WALLET  HOTKEY  PORT  PM2_NAME  [AGENT_NAME]"
# Use "-" for WALLET or AGENT_NAME to fall back to the defaults above.
#
# Cost-minimization profile: 1 miner, 1 hotkey, default agent.
# Add more rows once additional hotkeys are registered.
# ----------------------------------------------------------------
MINERS=(
  # wallet              hotkey            port   pm2-name           agent
  "superbit-darnsin     sn79-miner-05040     9001   sn79_miner_1       -"
  # Examples (uncomment and customize when ready):
  # "-                  sn79-miner-2      8092   sn79_miner_2       RandomMakerAgent"
  # "-                  sn79-miner-3      8093   sn79_miner_3       ImbalanceAgent"
)

# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
require_pm2() {
  if ! command -v pm2 >/dev/null 2>&1; then
    echo "Error: pm2 is not installed. Run scripts/setup/install_vps.sh or: npm install -g pm2"
    exit 1
  fi
}

require_python() {
  if [ ! -x "$PYTHON" ]; then
    echo "Error: python interpreter not found at $PYTHON"
    echo "Run scripts/setup/install_vps.sh first to create the venv, or set PYTHON=/path/to/python."
    exit 1
  fi
  if [ ! -f "$MINER_SCRIPT" ]; then
    echo "Error: miner script not found at $MINER_SCRIPT"
    exit 1
  fi
  if [ ! -d "$AGENT_PATH" ]; then
    echo "Error: agent path $AGENT_PATH does not exist."
    echo "Run scripts/setup/install_vps.sh first, or copy agents/*.py there manually."
    exit 1
  fi
}

# Parse a miner entry into named variables.
# Sets: _WALLET, _HOTKEY, _PORT, _PM2_NAME, _AGENT_NAME, _AGENT_PARAMS
parse_miner_entry() {
  _WALLET=""; _HOTKEY=""; _PORT=""; _PM2_NAME=""; _AGENT_NAME=""
  read -r _WALLET _HOTKEY _PORT _PM2_NAME _AGENT_NAME <<< "$1"
  if [ -z "$_WALLET" ] || [ "$_WALLET" = "-" ]; then
    _WALLET="$DEFAULT_WALLET_NAME"
  fi
  if [ -z "$_AGENT_NAME" ] || [ "$_AGENT_NAME" = "-" ]; then
    _AGENT_NAME="$DEFAULT_AGENT_NAME"
  fi
  _AGENT_PARAMS="${AGENT_PARAMS[$_AGENT_NAME]:-}"
}

# Build the bittensor + agent CLI args.
miner_args() {
  local wallet="$1"
  local hotkey="$2"
  local port="$3"
  local agent_name="$4"
  local agent_params="$5"

  local args="--netuid $NETUID \
        --subtensor.chain_endpoint $SUBTENSOR_ENDPOINT \
        --wallet.path $WALLET_PATH \
        --wallet.name $wallet \
        --wallet.hotkey $hotkey \
        --axon.port $port \
        --agent.path $AGENT_PATH \
        --agent.name $agent_name \
        --logging.$LOG_LEVEL"

  # Advertise the correct public IP so validators can reach the axon.
  # Critical on VPS / cloud where the NIC shows a private (NAT'd) IP.
  if [ -n "$AXON_EXTERNAL_IP" ]; then
    args="$args --axon.external_ip $AXON_EXTERNAL_IP"
  fi

  # --agent.params uses nargs='*' so unquoted expansion is correct here.
  if [ -n "$agent_params" ]; then
    args="$args --agent.params $agent_params"
  fi

  echo "$args"
}

# ----------------------------------------------------------------
# Commands
# ----------------------------------------------------------------
start_all() {
  require_pm2
  require_python
  export PYTHONPATH="$REPO_ROOT"

  if [ -z "$AXON_EXTERNAL_IP" ]; then
    echo "WARN: AXON_EXTERNAL_IP not set. If this VPS has a private NIC IP, validators won't reach the axon."
    echo "      Re-run with:  AXON_EXTERNAL_IP=<your.public.ip> $0 start"
    echo
  fi

  for entry in "${MINERS[@]}"; do
    parse_miner_entry "$entry"
    echo "Starting miner: pm2=$_PM2_NAME  wallet=$_WALLET  hotkey=$_HOTKEY  port=$_PORT  agent=$_AGENT_NAME"
    pm2 delete "$_PM2_NAME" 2>/dev/null || true

    # shellcheck disable=SC2046
    pm2 start "$MINER_SCRIPT" \
      --name "$_PM2_NAME" \
      --interpreter "$PYTHON" \
      --cwd "$REPO_ROOT/taos/im/neurons" \
      --time \
      -- $(miner_args "$_WALLET" "$_HOTKEY" "$_PORT" "$_AGENT_NAME" "$_AGENT_PARAMS")
  done
  pm2 save
  echo
  echo "All miners started. Check status: pm2 list"
  echo "Tail one:                          pm2 logs <pm2_name>"
}

stop_all() {
  require_pm2
  for entry in "${MINERS[@]}"; do
    parse_miner_entry "$entry"
    echo "Stopping: $_PM2_NAME"
    pm2 stop "$_PM2_NAME" 2>/dev/null || echo "  (already stopped)"
  done
}

restart_all() {
  require_pm2
  for entry in "${MINERS[@]}"; do
    parse_miner_entry "$entry"
    echo "Restarting: $_PM2_NAME  (wallet=$_WALLET  agent=$_AGENT_NAME)"
    pm2 restart "$_PM2_NAME" --update-env 2>/dev/null \
      || echo "  ($_PM2_NAME not found - run 'start' first)"
  done
}

status_all() {
  require_pm2
  pm2 list
}

logs_one() {
  local name="$1"
  if [ -z "$name" ]; then
    echo "Usage: $0 logs <pm2_name>"
    echo "Available miners:"
    for entry in "${MINERS[@]}"; do
      parse_miner_entry "$entry"
      echo "  $_PM2_NAME  (wallet=$_WALLET  hotkey=$_HOTKEY  port=$_PORT  agent=$_AGENT_NAME)"
    done
    exit 1
  fi
  pm2 logs "$name"
}

list_miners() {
  echo "Configured miners:"
  echo
  printf "  %-22s %-22s %-6s %-22s %s\n" "WALLET" "HOTKEY" "PORT" "PM2_NAME" "AGENT"
  printf "  %-22s %-22s %-6s %-22s %s\n" "------" "------" "----" "--------" "-----"
  for entry in "${MINERS[@]}"; do
    parse_miner_entry "$entry"
    printf "  %-22s %-22s %-6s %-22s %s\n" "$_WALLET" "$_HOTKEY" "$_PORT" "$_PM2_NAME" "$_AGENT_NAME"
  done
  echo
  echo "Defaults: wallet=$DEFAULT_WALLET_NAME  agent=$DEFAULT_AGENT_NAME  netuid=$NETUID"
  echo "Endpoint: $SUBTENSOR_ENDPOINT"
  echo "Axon ext: ${AXON_EXTERNAL_IP:-<unset, will auto-detect>}"
  echo "Python:   $PYTHON"
  echo "Miner:    $MINER_SCRIPT"
  echo "Agents:   $AGENT_PATH"
}

# ----------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------
case "${1:-}" in
  start)    start_all ;;
  stop)     stop_all ;;
  restart)  restart_all ;;
  status)   status_all ;;
  logs)     logs_one "${2:-}" ;;
  list)     list_miners ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs <pm2_name>|list}"
    echo
    list_miners
    exit 1
    ;;
esac
