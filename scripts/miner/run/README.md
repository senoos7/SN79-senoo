# SN-79 (taos.im) miner — VPS deployment

Cost-minimization profile: 1 hotkey, 1 miner, default sample agent, single VPS.

---

## 0. Prerequisites on the VPS

- Ubuntu 22.04 or 24.04
- `sudo` privileges (or root)
- Public IP **with the axon port open inbound** (default `8091`)
- ~2 GB free RAM, ~5 GB disk (way under the 16-CPU / 32-GB box you have)

Open the firewall port (cloud provider security group **and** the VPS itself):

```bash
sudo ufw allow 8091/tcp        # if ufw is active; otherwise iptables / cloud SG
```

---

## 1. Clone the repo and run the bootstrap

```bash
cd ~
git clone https://github.com/taos-im/sn-79.git
cd sn-79
./scripts/setup/install_vps.sh
```

This installs (idempotently):

- apt build deps, `tmux`, `htop`, `jq`
- Node.js LTS, `pm2`, `pm2-logrotate`
- `uv`, then a Python 3.10.9 venv at `./.venv`
- The `taos` package (`pip install -e .`)
- (Optional) populates the legacy `~/.taos/agents` for compatibility with the
  upstream `run_miner.sh`. The new `run_multi_miner_sn79.sh` reads agents
  directly from the repo's `agents/` directory, so a `git pull` is enough to
  get new agents — no copy step needed.

Smoke test at the end imports `taos` + `bittensor` and validates the custom
`AdaptiveMarketMakerAgent.py` parses cleanly.

---

## 2. Create / register your hotkey

You can either copy an existing wallet to the VPS:

```bash
# from your laptop (where the cold key already exists):
scp -r ~/.bittensor/wallets/<coldkey_name> user@vps:~/.bittensor/wallets/
```

…or create one fresh on the VPS:

```bash
source ~/sn-79/.venv/bin/activate
btcli wallet new_coldkey  --wallet.name superbit-darnsin
btcli wallet new_hotkey   --wallet.name superbit-darnsin --wallet.hotkey sn79-miner-1
```

Check current SN-79 registration cost first (cheapest possible spend):

```bash
btcli subnet burn_cost --netuid 79
btcli subnets show     --netuid 79
```

Register on **testnet 366** first (free TAO from Bittensor Discord) to validate end-to-end:

```bash
btcli subnet register --netuid 366 \
  --subtensor.network test \
  --wallet.name superbit-darnsin --wallet.hotkey sn79-miner-1
```

When confident, register on mainnet:

```bash
btcli subnet register --netuid 79 \
  --wallet.name superbit-darnsin --wallet.hotkey sn79-miner-1
```

---

## 3. Configure and launch the miner

Edit `scripts/miner/run/run_multi_miner_sn79.sh` if your wallet/hotkey names differ
from the defaults. The default row is:

```bash
MINERS=(
  # wallet              hotkey            port   pm2-name           agent
  "-                    sn79-miner-1      8091   sn79_miner_1       -"
)
```

`-` means "use `DEFAULT_WALLET_NAME` / `DEFAULT_AGENT_NAME`".

Start the miner. By default the script **auto-detects this VPS's public IPv4**
by hitting `api.ipify.org` / `ifconfig.me` / `ipinfo.io` / `icanhazip.com` /
`checkip.amazonaws.com` (first valid response wins) and passes it as
`--axon.external_ip`. This avoids the common VPS pitfall where the NIC has a
private NAT'd IP and validators can't reach the axon.

```bash
cd ~/sn-79
./scripts/miner/run/run_multi_miner_sn79.sh start
```

You can still override manually:

```bash
# force a specific IP (e.g. behind a proxy / floating IP setup)
AXON_EXTERNAL_IP=1.2.3.4 ./scripts/miner/run/run_multi_miner_sn79.sh start

# OR disable auto-detect and let bittensor pick from NIC
# (only safe when the NIC already shows the public IP)
AXON_EXTERNAL_IP="" ./scripts/miner/run/run_multi_miner_sn79.sh start
```

Other commands:

```bash
./scripts/miner/run/run_multi_miner_sn79.sh list      # show configured rows
./scripts/miner/run/run_multi_miner_sn79.sh status    # pm2 list
./scripts/miner/run/run_multi_miner_sn79.sh logs sn79_miner_1
./scripts/miner/run/run_multi_miner_sn79.sh restart
./scripts/miner/run/run_multi_miner_sn79.sh stop
```

Persist across reboots:

```bash
pm2 save
pm2 startup    # follow the printed sudo command
```

---

## 4. Verify the miner is healthy

1. **pm2 says `online`** — `pm2 list` shows the process running, no rapid restarts.
2. **Logs show queries arriving** — `pm2 logs sn79_miner_1` should show entries
   like `Decompressed (...s)` and `Response Generated in ...s` once a validator
   queries you (may take minutes after register).
3. **Dashboard shows your UID** —
   - Mainnet: <https://taos.simulate.trading> (and the new <https://mvtrx.simulate.trading>)
   - Testnet: <https://testnet.simulate.trading>
   Click your UID in the **Agents** table; check:
   - Recent **trades on every book** (need to be active on all to score)
   - Mostly **success** under Requests (no high timeout rate)
   - **Kappa-3 Score** trending positive over 24h

If you see no queries: 99% of the time it's the public IP / firewall.
Re-check `AXON_EXTERNAL_IP` and that the port is reachable from outside
(`curl http://<your.public.ip>:8091/` should at least connect).

---

## 5. Switching agents

The shipped sample agents (in the repo's `agents/` directory) are not expected
to win — see the SN-79 README and FAQ. They're fine to **keep your hotkey
alive and producing activity** while you develop a better one.

To switch to a different sample, change the 5th column of the miner row in
`run_multi_miner_sn79.sh`:

```bash
"-  sn79-miner-1  8091  sn79_miner_1  RandomMakerAgent"
```

To deploy a **custom agent**:

1. Drop `MyAgent.py` into the repo's `agents/` directory (or scp it from your
   dev box). It must define a class `MyAgent` inheriting from
   `FinanceSimulationAgent`.
2. Add an entry to the `AGENT_PARAMS` associative array at the top of
   `run_multi_miner_sn79.sh`:

   ```bash
   declare -A AGENT_PARAMS=(
     ...
     [MyAgent]="my_param=0.5 other_param=42"
   )
   ```

3. Set the miner row to use it and `restart`:

   ```bash
   "-  sn79-miner-1  8091  sn79_miner_1  MyAgent"
   ```

   ```bash
   ./scripts/miner/run/run_multi_miner_sn79.sh restart
   ```

---

## 6. Cost summary (this profile)

| Item | One-time | Recurring |
|---|---|---|
| Registration (recycled TAO) | ~0.01 – 1 TAO (volatile) | only on dereg |
| VPS (your existing 16C / 32GB shared) | $0 | already paid |
| TAO buffer for re-registration | 1–2 TAO | when needed |

Total minimum to **start** mining: cost of one mainnet registration ≈ a few USD
to ~$300 depending on the live `burn_cost` at the moment you register.

---

## 7. Custom agent: `AdaptiveMarketMakerAgent`

This repo ships with a custom competitive agent at
`agents/AdaptiveMarketMakerAgent.py`. It is the **default** in
`run_multi_miner_sn79.sh` (you don't need to switch to it manually).

### What it does

An inventory-managed limit-order market maker, designed against the SN-79 reward
function in `taos/im/validator/reward.py`:

- **Fair value = microprice** (queue-weighted), not midquote.
- **Reservation price** `r = micro − γ·inv·σ²` — Avellaneda-Stoikov style: the
  more long we are, the lower we quote (and vice versa). This naturally pulls
  inventory back to zero, which is what Kappa-3 (realized round-trip P&L) rewards.
- **Half-spread** `h = base_half_spread·mid + γ·σ²·mid` — widens automatically
  in volatile periods so we don't get picked off.
- **Quotes both sides** as `postOnly` GTT limits with short expiry; this
  produces natural turnover without a separate cancellation loop.
- **Smart re-quote**: only cancel/replace existing orders when the target moves
  more than `requote_tolerance_pct` of mid. Avoids cancel-to-fill ratio penalty
  if SN-79 introduces one in future.
- **Hard stop-loss in `onTrade`**: when unrealized loss on a book exceeds
  `stop_loss_pct × per_book_capital`, market-flatten the inventory immediately.
  This is the single most important Kappa-3 protector — `LPM₃` punishes
  realized losses cubically.
- **Volume gate**: when `account.traded_volume` on a book exceeds
  `vol_pause_pct × cap`, pull all quotes for that book until the rolling
  window decays. Prevents the validator-side lockout.
- **Identical params per book**: critical for the IQR outlier penalty.

### Tuning knobs (`AGENT_PARAMS[AdaptiveMarketMakerAgent]` in the run script)

| Param | Default | What it does |
|---|---:|---|
| `base_qty` | `0.5` | Order size in BASE per side. Scale to ~1% of per-book capital / mid_price. |
| `base_half_spread` | `0.0008` | Static component of half-spread. 8 bps default. |
| `gamma` | `8.0` | Inventory aversion + vol-driven spread widening. `gamma=20` blew quoted spreads to 20%+ on noisy books → no fills. Lower if you see persistent wide quotes. |
| `vol_window` | `64` | EWMA span (samples) for σ² estimation. |
| `inventory_cap_pct` | `0.50` | Hard cap on \|inventory\| / per-book capital. Lower values lock us into flatten-only mode after one fill if base_qty is large relative to per-book wealth → kills 24H volume. |
| `stop_loss_pct` | `0.005` | Flatten inventory if MTM loss > 0.5% of per-book capital. |
| `vol_pause_pct` | `0.70` | Stop quoting once traded_volume reaches 70% of cap. |
| `expiry_seconds` | `30` | GTT expiry (sim seconds). MUST exceed publish interval + validator latency. |
| `max_half_spread_pct` | `0.005` | Ceiling on half-spread (0.5% of mid). Prevents EWMA blowups on noisy books from quoting 20%+ wide and never filling. |
| `requote_tolerance_pct` | `0.0015` | Re-quote only if target shifts > 15 bps. Bigger = fewer instructions per response = faster decompression on validator side = lower latency penalty. |
| `capital_turnover_cap` | `1.0` | Mirror of `scoring.activity.capital_turnover_cap`. |
| `diag_every` | `20` | Emit a one-line `DIAG c=...` log every N cycles showing miner_wealth, per-book wealth, # books quoted/skipped, inventory distribution, response time. Set to a large number to silence. |

To change a knob, edit the `AGENT_PARAMS` row in
`scripts/miner/run/run_multi_miner_sn79.sh` and `restart`.

### Tuning workflow

1. **Observe**: after running for an hour, look at the dashboard for your UID:
   - **Are you trading on every book?** If a book stays silent for >30 min,
     `base_qty` may be too small relative to inventory_cap, or quotes are too
     wide. Lower `base_half_spread`.
   - **Are you over the volume cap?** Lower `base_qty` or raise
     `vol_pause_pct` ceiling. Better to be at 60% of cap than locked out.
   - **Is realized P&L negative?** Likely fee drag. Either widen
     `base_half_spread` or be more selective about when to quote (skip when
     `σ²` is high).
   - **Are stop-losses firing?** If too often, increase `stop_loss_pct` or
     reduce `gamma` so you carry less inventory in the first place.
2. **Iterate** on testnet 366 first. Don't tune against mainnet — `burn_cost`
   pays for that.

### Local test against the simulator (optional but recommended)

The repo includes an offline proxy that runs the same C++ simulation engine
without the Bittensor layer:

```bash
# (one-time) install the simulator on a dev box (NOT the VPS necessarily)
cd ~/sn-79
sudo ./install_simulator.sh           # ~2h on Ubuntu 22.04 the first time
source .venv/bin/activate
pip install -e .

# Edit agents/proxy/config.json so it loads AdaptiveMarketMakerAgent
# Then in three terminals (or use the launcher):

# 1) Proxy
cd agents/proxy && python proxy.py --config config.json

# 2) Agent
cd agents && python AdaptiveMarketMakerAgent.py --port 8888 --agent_id 0 \
    --params base_qty=0.5 base_half_spread=0.0008 gamma=8.0 \
             stop_loss_pct=0.005 vol_pause_pct=0.70 expiry_seconds=30 \
             max_half_spread_pct=0.005 requote_tolerance_pct=0.0015

# 3) Simulator
cd simulate/trading/run && ../build/src/cpp/taosim -f config/simulation_0.xml
```

Watch agent logs: it should log `respond() Xms books=N instructions=M` and
periodic `STOP-LOSS firing` / `volume-gated` lines. If you see
`respond() took >0.25s` warnings, lower the per-cycle workload (smaller
`vol_window`, fewer books in proxy config).

---

## 8. Where to go further

- Read `agents/README.md` end-to-end — it documents every instruction type, the
  state-update structure, the volume cap, and the latency mechanic.
- Read `taos/im/validator/reward.py` — that's the *exact* score function
  (Kappa-3 × activity_factor × pnl_factor, median across books, IQR outlier
  penalty, Pareto-distributed final reward).
- Iterate on `AdaptiveMarketMakerAgent`. Promising directions:
  - Add an OFI signal from `book.events` (L3 stream) to bias `reservation`.
  - Online-learn σ² regimes per book (e.g. detect high-vol periods and pause).
  - Add multiple price levels per side (deeper quotes at wider spreads).
  - Switch `base_qty` to scale with order book depth (`qty ∝ best_bid_qty`).
- Watch the upcoming `taos.im` features the owner announced
  (ExeTRX SL/TP, batch order submission, GenTRX). Each lets you remove or
  speed up code in this agent.
