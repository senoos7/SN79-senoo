# SPDX-License-Identifier: MIT
"""
DirectionalMomentumAgent

A signal-driven directional trader for SN-79 (taos.im). Designed as the Phase 2
counterpart to AdaptiveMarketMakerAgent: instead of continuously quoting both
sides (and getting adversely selected on every trend), this agent takes
directional positions based on a composite alpha signal and holds them while
the signal persists. Closes for profit when the signal reverses.

Why this fits the SN-79 reward function
=======================================
Recall:
  final_score = kappa_weight * kappa_score + pnl_weight * pnl_score
  kappa_score = median_over_books(activity * pnl_factor * normalized_kappa)
  pnl_score    in [-0.5, +0.5]   -- direct hit on negative realized PnL.

Empirical observation from rank-#2 immune miners on the live subnet:
  - Total volume ~ $25k / day
  - Round-trip volume ~ $400 / day  (~1.5% of total)
  - Realized PnL ~ +$12k / day  (positive!)
  - Inventory carried in QUOTE ~ $1M (large directional bets)

Translation: they don't make markets. They take directional positions, hold
while the alpha persists, and harvest profits at favorable prices. The few
round-trips they DO close are highly profitable because the move was real.

Strategy
========
Each cycle, per book:

  1. Compute alpha = w_obi * OBI + w_mom * mom_signal  in [-1, +1]
       OBI         = (bid_qty - ask_qty) / (bid_qty + ask_qty)   -- flow pressure
       mom_signal  = clamp((short_ewma/long_ewma - 1) / mom_norm, -1, +1)

  2. Smooth alpha with an EWMA so we don't whipsaw on noise:
       alpha_smooth = (1-alpha_smooth_a) * alpha_smooth + alpha_smooth_a * alpha_raw

  3. Map smoothed alpha to a target inventory:
       target = sign(alpha) * |alpha|^signal_power * target_max_base_per_book
       (|alpha| < signal_threshold  =>  target = 0, i.e. flat)

  4. Determine desired action:
       delta = target - current_inventory
       If |delta| > min_chunk: place a single chunk-sized limit order in
       direction of delta. We use postOnly+GTT so we never pay the spread.
       Order placement aggression scales with |alpha|:
         - |alpha| in [threshold, .5)  -> patient (passive at bid/ask)
         - |alpha| in [.5, .8)         -> moderate (one tick inside touch)
         - |alpha| >= .8               -> aggressive (cross slightly when valid)

  5. Take-profit: when position is in profit AND alpha is weakening
     (|alpha| < take_profit_alpha_thresh), place an exit limit at favorable
     price (mid +/- exit_offset_bps) to harvest the move.

  6. Per-book stop-loss: market-flatten when unrealized loss > stop_loss_pct
     of per-book wealth. Identical safety layer to the MM agent.

Tunable parameters (all overridable via `--agent.params key=val`)
================================================================
Position sizing:
  target_max_base_per_book = 1.0    max BASE we'll hold per book (cap on |inv|)
  chunk_qty                = 0.2    BASE per single limit-order placement
  min_delta                = 0.1    don't act unless |target - inv| exceeds this

Signal:
  obi_weight               = 0.5    weight of order-book imbalance in alpha
  mom_weight               = 0.5    weight of momentum in alpha (sum should be 1.0)
  mom_norm_bps             = 15.0   momentum scale: 15 bps = saturation
  ewma_short_alpha         = 0.30   short EWMA of microprice (~3-cycle half-life)
  ewma_long_alpha          = 0.05   long EWMA of microprice (~14-cycle half-life)
  signal_threshold         = 0.15   |alpha| below this -> target inventory = 0
  signal_power             = 1.5    target sizing curve: > 1.0 dampens weak signals
  alpha_smooth_alpha       = 0.4    EWMA on alpha itself (anti-whipsaw)

Order placement:
  entry_aggressive_alpha   = 0.8    above this |alpha|, quote AT touch (top of queue)
  entry_moderate_alpha     = 0.5    above this, quote one tick INSIDE bid/ask
  expiry_seconds           = 30     GTT expiry (sim seconds) on placed orders
  requote_tolerance_pct    = 0.0015 only re-quote when target shifts > 15 bps of mid

Take profit:
  take_profit_alpha_thresh = 0.10   exit when |alpha| drops below this
  exit_offset_bps          = 8.0    place exit at mid +/- 8 bps from cost basis

Risk:
  stop_loss_pct            = 0.01   per-book stop-loss as fraction of per-book wealth
  max_volatility_sigma     = 0.005  pause trading when EWMA std of returns exceeds this

Diagnostics:
  diag_every               = 20     emit a one-line DIAG summary every N cycles
"""
from __future__ import annotations

import math
import time
from typing import Dict

import bittensor as bt

from taos.common.agents import launch
from taos.im.agents import FinanceSimulationAgent
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.events import TradeEvent, SimulationStartEvent
from taos.im.protocol.models import (
    OrderDirection,
    STP,
    TimeInForce,
)


# ---------------------------------------------------------------------------
# Per-book persistent state
# ---------------------------------------------------------------------------
# Plain class with __slots__ (NOT @dataclass) -- the miner base loader uses
# importlib without registering the module in sys.modules, which crashes
# @dataclass introspection at class-construction time.
class BookState:
    """Persistent per-book state surviving across state updates within a simulation."""

    __slots__ = (
        "inventory",          # Net BASE position from our trades since start
        "avg_cost",           # Volume-weighted avg entry price of current position
        "cum_realized_pnl",   # Cumulative realized PnL on this book this simulation
        "ewma_short",         # Short-window EWMA of microprice (signal numerator)
        "ewma_long",          # Long-window EWMA of microprice (signal denominator)
        "alpha_smooth",       # EWMA of alpha to dampen whipsaw
        "target_inv",         # Current target inventory (signed BASE)
        "last_buy_price",     # Last quoted buy price (for re-quote hysteresis)
        "last_sell_price",    # Last quoted sell price
        "stop_armed",         # Stop-loss armed (briefly disarmed after firing)
        "sigma2",             # EWMA variance of log-returns (volatility regime)
        "last_mid",           # Previous mid for return calc
    )

    def __init__(self) -> None:
        self.inventory = 0.0
        self.avg_cost = 0.0
        self.cum_realized_pnl = 0.0
        self.ewma_short = 0.0
        self.ewma_long = 0.0
        self.alpha_smooth = 0.0
        self.target_inv = 0.0
        self.last_buy_price = 0.0
        self.last_sell_price = 0.0
        self.stop_armed = True
        self.sigma2 = 0.0
        self.last_mid = 0.0


# ---------------------------------------------------------------------------
# DirectionalMomentumAgent
# ---------------------------------------------------------------------------
class DirectionalMomentumAgent(FinanceSimulationAgent):
    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def initialize(self):
        c = self.config
        # ----- Position sizing -----
        self.target_max_base_per_book: float = float(getattr(c, "target_max_base_per_book", 1.0))
        self.chunk_qty: float = float(getattr(c, "chunk_qty", 0.2))
        self.min_delta: float = float(getattr(c, "min_delta", 0.1))
        # ----- Signal weights -----
        self.obi_weight: float = float(getattr(c, "obi_weight", 0.5))
        self.mom_weight: float = float(getattr(c, "mom_weight", 0.5))
        self.mom_norm_bps: float = float(getattr(c, "mom_norm_bps", 15.0))
        self.ewma_short_alpha: float = float(getattr(c, "ewma_short_alpha", 0.30))
        self.ewma_long_alpha: float = float(getattr(c, "ewma_long_alpha", 0.05))
        # |alpha| below this -> target inventory = 0 (stay flat).
        # Empirically the smoothed alpha hovers in [0.07, 0.30] with rare
        # spikes higher. 0.15 filtered out almost every directional cycle.
        # 0.08 lets us enter modest positions on most cycles while still
        # rejecting near-zero noise.
        self.signal_threshold: float = float(getattr(c, "signal_threshold", 0.08))
        self.signal_power: float = float(getattr(c, "signal_power", 1.5))
        self.alpha_smooth_alpha: float = float(getattr(c, "alpha_smooth_alpha", 0.4))
        # ----- Order placement -----
        self.entry_aggressive_alpha: float = float(getattr(c, "entry_aggressive_alpha", 0.8))
        self.entry_moderate_alpha: float = float(getattr(c, "entry_moderate_alpha", 0.5))
        # GTT expiry in REAL seconds (×1e9 = nanoseconds passed to API).
        # Reference ImbalanceAgent uses 120 real seconds. Taker orders fill
        # immediately so this is a safety TTL for any unfilled residuals.
        self.expiry_seconds: int = int(getattr(c, "expiry_seconds", 120))
        self.requote_tolerance_pct: float = float(getattr(c, "requote_tolerance_pct", 0.0015))
        # ----- Take profit -----
        self.take_profit_alpha_thresh: float = float(getattr(c, "take_profit_alpha_thresh", 0.10))
        self.exit_offset_bps: float = float(getattr(c, "exit_offset_bps", 8.0))
        # ----- Risk -----
        self.stop_loss_pct: float = float(getattr(c, "stop_loss_pct", 0.01))
        # EWMA std of per-REAL-CYCLE log-returns above which we sit out a book.
        # The SN-79 sim runs at ~88x real-time (empirically: ~600 sim sec per
        # 7 real sec). That means even a "normal" asset shows 10-50% per-cycle
        # returns, making sigma >> any reasonable threshold. Setting to 1.0
        # (100% per cycle) effectively disables this gate; any extreme blowup
        # large enough to trigger it would be a genuine anomaly.
        self.max_volatility_sigma: float = float(getattr(c, "max_volatility_sigma", 1.0))
        # ----- Volatility EWMA samples (for regime gate) -----
        self.vol_window: int = int(getattr(c, "vol_window", 64))

        # Per-book persistent state. Reset on simulation start.
        self.book_state: Dict[int, BookState] = {}
        self._cycle: int = 0
        self._diag_every: int = int(getattr(c, "diag_every", 20))

        bt.logging.info(
            "DirectionalMomentumAgent initialized | "
            f"target_max_base={self.target_max_base_per_book} chunk_qty={self.chunk_qty} "
            f"min_delta={self.min_delta} obi_w={self.obi_weight} mom_w={self.mom_weight} "
            f"mom_norm_bps={self.mom_norm_bps} signal_thresh={self.signal_threshold} "
            f"signal_power={self.signal_power} alpha_smooth_a={self.alpha_smooth_alpha} "
            f"entry_aggr_alpha={self.entry_aggressive_alpha} entry_mod_alpha={self.entry_moderate_alpha} "
            f"tp_alpha={self.take_profit_alpha_thresh} exit_offset_bps={self.exit_offset_bps} "
            f"stop_loss_pct={self.stop_loss_pct} max_vol_sigma={self.max_volatility_sigma} "
            f"expiry_s={self.expiry_seconds} diag_every={self._diag_every}"
        )

    # ------------------------------------------------------------------
    # Event hooks
    # ------------------------------------------------------------------
    def onStart(self, event: SimulationStartEvent) -> None:
        """Reset per-book state at the start of a fresh simulation."""
        bt.logging.info("Simulation start: clearing per-book state")
        self.book_state.clear()

    def onTrade(self, event: TradeEvent, validator: str = None) -> None:
        """Track inventory and realized PnL on each fill."""
        bs = self.book_state.setdefault(event.bookId, BookState())

        is_taker = (event.takerAgentId == self.uid)
        is_maker = (event.makerAgentId == self.uid)
        if not (is_taker or is_maker):
            return

        taker_side = OrderDirection(event.side)
        if is_taker:
            our_side = taker_side
        else:
            our_side = OrderDirection.SELL if taker_side == OrderDirection.BUY else OrderDirection.BUY

        qty = float(event.quantity)
        px = float(event.price)
        signed_qty = qty if our_side == OrderDirection.BUY else -qty

        prev_inv = bs.inventory
        new_inv = prev_inv + signed_qty

        # Realized PnL crystallizes on the closing leg (sign change or |inv| shrinks).
        if prev_inv != 0.0 and (new_inv == 0.0 or (prev_inv * new_inv) < 0.0 or abs(new_inv) < abs(prev_inv)):
            closed = min(abs(prev_inv), qty)
            sign = 1.0 if prev_inv > 0 else -1.0
            pnl = (px - bs.avg_cost) * sign * closed
            bs.cum_realized_pnl += pnl

        # Update average cost.
        if (prev_inv == 0.0) or (prev_inv * signed_qty > 0):
            total_qty = abs(prev_inv) + qty
            if total_qty > 0:
                bs.avg_cost = (abs(prev_inv) * bs.avg_cost + qty * px) / total_qty
        elif (prev_inv * new_inv) < 0:
            bs.avg_cost = px
        elif new_inv == 0.0:
            bs.avg_cost = 0.0

        bs.inventory = new_inv

    def onOrderRejected(self, event) -> None:
        super().onOrderRejected(event)
        bt.logging.warning(
            f"BOOK {getattr(event, 'bookId', '?')} order rejected: {getattr(event, 'message', '')}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _miner_wealth_per_book(self) -> float:
        cfg = self.simulation_config
        wealth = float(getattr(cfg, "miner_wealth", 0.0)) or 0.0
        n_books = max(1, int(getattr(cfg, "book_count", 1)))
        return wealth / n_books if wealth > 0 else 0.0

    def _round_price(self, p: float) -> float:
        d = int(getattr(self.simulation_config, "priceDecimals", 2))
        return round(p, d)

    def _round_qty(self, q: float) -> float:
        d = int(getattr(self.simulation_config, "volumeDecimals", 4))
        return round(q, d)

    def _update_signals(self, bs: BookState, mid: float, micro: float) -> None:
        """Update per-book volatility and signal EWMAs."""
        # Volatility (used for regime gate, not for sizing here).
        if bs.last_mid > 0 and mid > 0:
            r = math.log(mid / bs.last_mid)
            alpha = 2.0 / (self.vol_window + 1)
            bs.sigma2 = (1 - alpha) * bs.sigma2 + alpha * r * r
        bs.last_mid = mid
        # Signal EWMAs of microprice. Initialize to current micro on first cycle.
        if bs.ewma_short <= 0:
            bs.ewma_short = micro
            bs.ewma_long = micro
        else:
            bs.ewma_short = (1 - self.ewma_short_alpha) * bs.ewma_short + self.ewma_short_alpha * micro
            bs.ewma_long = (1 - self.ewma_long_alpha) * bs.ewma_long + self.ewma_long_alpha * micro

    def _compute_alpha(self, bs: BookState, bid_qty: float, ask_qty: float) -> float:
        """Composite alpha in [-1, +1] from OBI and microprice momentum."""
        # Order Book Imbalance.
        denom = bid_qty + ask_qty
        obi = (bid_qty - ask_qty) / denom if denom > 0 else 0.0
        obi = max(-1.0, min(1.0, obi))
        # Momentum: short EWMA vs long EWMA, normalized to [-1, +1].
        mom_signal = 0.0
        if bs.ewma_long > 0:
            mom_pct = (bs.ewma_short / bs.ewma_long) - 1.0
            mom_norm = self.mom_norm_bps * 1e-4
            if mom_norm > 0:
                mom_signal = max(-1.0, min(1.0, mom_pct / mom_norm))
        # Composite (assume w_obi + w_mom == 1, but normalize defensively).
        wsum = self.obi_weight + self.mom_weight
        if wsum <= 0:
            return 0.0
        alpha_raw = (self.obi_weight * obi + self.mom_weight * mom_signal) / wsum
        # Smooth to dampen whipsaw.
        if bs.alpha_smooth == 0.0:
            bs.alpha_smooth = alpha_raw
        else:
            bs.alpha_smooth = (1 - self.alpha_smooth_alpha) * bs.alpha_smooth + self.alpha_smooth_alpha * alpha_raw
        return bs.alpha_smooth

    def _compute_target_inventory(self, alpha: float) -> float:
        """Map smoothed alpha to a signed target inventory in BASE."""
        if abs(alpha) < self.signal_threshold:
            return 0.0
        sign = 1.0 if alpha > 0 else -1.0
        magnitude = abs(alpha) ** self.signal_power
        return sign * magnitude * self.target_max_base_per_book

    def _maybe_stop_loss(
        self,
        response: FinanceAgentResponse,
        book_id: int,
        bs: BookState,
        mid: float,
    ) -> bool:
        """Market-flatten when per-book unrealized loss exceeds the threshold."""
        if bs.inventory == 0.0 or not bs.stop_armed:
            return False
        per_book_cap = self._miner_wealth_per_book()
        if per_book_cap <= 0:
            return False
        unrealized = (mid - bs.avg_cost) * (1.0 if bs.inventory > 0 else -1.0) * abs(bs.inventory)
        loss_threshold = -self.stop_loss_pct * per_book_cap
        if unrealized < loss_threshold:
            qty = self._round_qty(abs(bs.inventory))
            if qty > 0:
                direction = OrderDirection.SELL if bs.inventory > 0 else OrderDirection.BUY
                response.market_order(book_id=book_id, direction=direction, quantity=qty)
                bt.logging.warning(
                    f"BOOK {book_id} STOP-LOSS: inv={bs.inventory:+.4f} avg_cost={bs.avg_cost:.4f} "
                    f"mid={mid:.4f} unrealized={unrealized:.4f} (< {loss_threshold:.4f})"
                )
                bs.stop_armed = False
                return True
        else:
            bs.stop_armed = True
        return False

    # ------------------------------------------------------------------
    # Main strategy
    # ------------------------------------------------------------------
    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        t0 = time.time()
        response = FinanceAgentResponse(agent_id=self.uid)
        priceDecimals = int(getattr(self.simulation_config, "priceDecimals", 2))
        expiry_ns = self.expiry_seconds * 1_000_000_000
        self._cycle += 1

        # Diagnostic counters.
        diag = {
            "books": 0,
            "stop_loss": 0,
            "vol_gated": 0,
            "max_sigma_seen": 0.0,   # highest sigma observed this cycle (for tuning)
            "long_target": 0, "short_target": 0, "flat_target": 0,
            "inv_long_books": 0, "inv_short_books": 0, "inv_flat_books": 0,
            "buy_placed": 0, "sell_placed": 0,
            "buy_kept": 0, "sell_kept": 0,
            "tp_placed": 0,
            "cancels": 0,
            "skipped_balance": 0,
            "sum_inv_base": 0.0,
            "sum_realized_pnl": 0.0,
            "sum_unrealized_pnl": 0.0,
            "abs_alpha_sum": 0.0,
            "abs_alpha_n": 0,
        }

        for book_id, book in state.books.items():
            if not book.bids or not book.asks:
                continue
            bs = self.book_state.setdefault(book_id, BookState())
            account = self.accounts.get(book_id)
            if account is None:
                continue

            best_bid = book.bids[0]
            best_ask = book.asks[0]
            bid, bid_qty = float(best_bid.price), float(best_bid.quantity)
            ask, ask_qty = float(best_ask.price), float(best_ask.quantity)
            tick = 10 ** (-priceDecimals)
            if ask <= bid:
                ask = bid + tick
            qsum = bid_qty + ask_qty
            micro = (bid_qty * ask + ask_qty * bid) / qsum if qsum > 0 else 0.5 * (bid + ask)
            mid = 0.5 * (bid + ask)

            self._update_signals(bs, mid, micro)
            diag["books"] += 1
            diag["sum_inv_base"] += bs.inventory
            diag["sum_realized_pnl"] += bs.cum_realized_pnl
            if bs.inventory > 1e-9:
                diag["inv_long_books"] += 1
            elif bs.inventory < -1e-9:
                diag["inv_short_books"] += 1
            else:
                diag["inv_flat_books"] += 1

            # Stop-loss takes precedence over signal.
            if self._maybe_stop_loss(response, book_id, bs, mid):
                rest_ids = [o.id for o in account.orders] if account.orders else []
                if rest_ids:
                    response.cancel_orders(book_id=book_id, order_ids=rest_ids)
                    diag["cancels"] += len(rest_ids)
                diag["stop_loss"] += 1
                continue

            # Volatility regime gate: when a book is in a vol blowup, sit out.
            sigma = math.sqrt(bs.sigma2) if bs.sigma2 > 0 else 0.0
            if sigma > diag["max_sigma_seen"]:
                diag["max_sigma_seen"] = sigma
            if sigma > self.max_volatility_sigma:
                rest_ids = [o.id for o in account.orders] if account.orders else []
                if rest_ids:
                    response.cancel_orders(book_id=book_id, order_ids=rest_ids)
                    diag["cancels"] += len(rest_ids)
                diag["vol_gated"] += 1
                continue

            # Compute composite alpha + target inventory.
            alpha = self._compute_alpha(bs, bid_qty, ask_qty)
            target = self._compute_target_inventory(alpha)
            bs.target_inv = target
            diag["abs_alpha_sum"] += abs(alpha)
            diag["abs_alpha_n"] += 1
            if target > 1e-9:
                diag["long_target"] += 1
            elif target < -1e-9:
                diag["short_target"] += 1
            else:
                diag["flat_target"] += 1

            # Track unrealized PnL for diagnostics.
            if bs.inventory != 0.0 and bs.avg_cost > 0:
                upnl = (mid - bs.avg_cost) * (1.0 if bs.inventory > 0 else -1.0) * abs(bs.inventory)
                diag["sum_unrealized_pnl"] += upnl

            delta = target - bs.inventory
            existing_buys = [o for o in account.orders if int(o.side) == int(OrderDirection.BUY)]
            existing_sells = [o for o in account.orders if int(o.side) == int(OrderDirection.SELL)]

            # ----- Decide what to do this cycle -----
            # Three modes:
            #   (a) target = 0  AND inventory != 0  -> EXIT (place tp limit on opposite side)
            #   (b) |delta| > min_delta             -> ENTER/ROTATE (place limit toward target)
            #   (c) at target                       -> HOLD (cancel anything stale)

            place_buy_price = None
            place_buy_qty = None
            place_sell_price = None
            place_sell_qty = None
            is_take_profit = False

            if abs(target) < 1e-9 and abs(bs.inventory) >= self.min_delta:
                # ----- EXIT mode: close position as TAKER -----
                # Sell at BID to close long, buy at ASK to close short.
                # Taker exit guarantees fill immediately (ImbalanceAgent pattern).
                if bs.inventory > 0:
                    place_sell_price = bid   # taker sell → immediate fill
                    place_sell_qty = abs(bs.inventory)
                else:
                    place_buy_price = ask    # taker buy → immediate fill
                    place_buy_qty = abs(bs.inventory)
                is_take_profit = True
            elif abs(delta) > self.min_delta:
                # ----- ENTER/ROTATE mode: TAKER -----
                # Buy at ASK (crosses immediately), sell at BID (crosses immediately).
                # Root cause of all prior zero-fill runs: postOnly=True at bid/ask
                # rested orders nobody ever crossed. The reference ImbalanceAgent
                # uses BUY@ASK / SELL@BID without postOnly → instant fills.
                qty_to_chunk = min(self.chunk_qty, abs(delta))
                if delta > 0:
                    place_buy_price = ask    # taker buy at ask → immediate fill
                    place_buy_qty = qty_to_chunk
                else:
                    place_sell_price = bid   # taker sell at bid → immediate fill
                    place_sell_qty = qty_to_chunk

            # ----- Order management: cancel/keep existing, place new where needed -----
            tol = self.requote_tolerance_pct * mid

            # Buy side.
            stale_buy_ids = []
            keep_buy = False
            for o in existing_buys:
                if place_buy_price is None:
                    stale_buy_ids.append(o.id)
                elif abs(float(o.price) - place_buy_price) > tol or abs(float(o.quantity) - (place_buy_qty or 0)) > 1e-9:
                    stale_buy_ids.append(o.id)
                else:
                    keep_buy = True
            # Sell side.
            stale_sell_ids = []
            keep_sell = False
            for o in existing_sells:
                if place_sell_price is None:
                    stale_sell_ids.append(o.id)
                elif abs(float(o.price) - place_sell_price) > tol or abs(float(o.quantity) - (place_sell_qty or 0)) > 1e-9:
                    stale_sell_ids.append(o.id)
                else:
                    keep_sell = True

            if stale_buy_ids or stale_sell_ids:
                response.cancel_orders(book_id=book_id, order_ids=stale_buy_ids + stale_sell_ids)
                diag["cancels"] += len(stale_buy_ids) + len(stale_sell_ids)
            if keep_buy:
                diag["buy_kept"] += 1
            if keep_sell:
                diag["sell_kept"] += 1

            # Place new buy.
            # Place new buy (TAKER: buy at ask, crosses immediately).
            if place_buy_price is not None and place_buy_qty is not None and not keep_buy:
                price = self._round_price(place_buy_price)
                qty = self._round_qty(place_buy_qty)
                cost = qty * price
                if qty > 0 and account.quote_balance.free >= cost:
                    response.limit_order(
                        book_id=book_id,
                        direction=OrderDirection.BUY,
                        quantity=qty,
                        price=price,
                        timeInForce=TimeInForce.GTT,
                        expiryPeriod=expiry_ns,
                        stp=STP.CANCEL_BOTH,
                    )
                    diag["buy_placed"] += 1
                    if is_take_profit:
                        diag["tp_placed"] += 1
                    bs.last_buy_price = price
                else:
                    diag["skipped_balance"] += 1

            # Place new sell (TAKER: sell at bid, crosses immediately).
            if place_sell_price is not None and place_sell_qty is not None and not keep_sell:
                price = self._round_price(place_sell_price)
                qty = self._round_qty(place_sell_qty)
                if qty > 0 and account.base_balance.free >= qty:
                    response.limit_order(
                        book_id=book_id,
                        direction=OrderDirection.SELL,
                        quantity=qty,
                        price=price,
                        timeInForce=TimeInForce.GTT,
                        expiryPeriod=expiry_ns,
                        stp=STP.CANCEL_BOTH,
                    )
                    diag["sell_placed"] += 1
                    if is_take_profit:
                        diag["tp_placed"] += 1
                    bs.last_sell_price = price
                else:
                    diag["skipped_balance"] += 1

        elapsed = time.time() - t0
        if elapsed > 0.25:
            bt.logging.warning(
                f"DirectionalMomentum respond() took {elapsed:.3f}s "
                f"(books={len(state.books)}, instructions={len(response.instructions)})"
            )

        # Periodic diagnostic.
        if self._cycle % self._diag_every == 0:
            mw = float(getattr(self.simulation_config, "miner_wealth", 0.0))
            bc = int(getattr(self.simulation_config, "book_count", 1))
            avg_alpha = (diag["abs_alpha_sum"] / diag["abs_alpha_n"]) if diag["abs_alpha_n"] else 0.0
            bt.logging.info(
                f"DIAG c={self._cycle} miner_wealth={mw:.0f} book_count={bc} "
                f"per_book_wealth={mw/max(bc,1):.2f} | "
                f"books={diag['books']} stop_loss={diag['stop_loss']} "
                f"vol_gated={diag['vol_gated']} max_sigma={diag['max_sigma_seen']:.3f} "
                f"vol_thresh={self.max_volatility_sigma:.3f} | "
                f"target: long={diag['long_target']} short={diag['short_target']} flat={diag['flat_target']} "
                f"avg_|alpha|={avg_alpha:.3f} | "
                f"inv: long={diag['inv_long_books']} short={diag['inv_short_books']} "
                f"flat={diag['inv_flat_books']} sum={diag['sum_inv_base']:+.2f} | "
                f"placed: buy={diag['buy_placed']} sell={diag['sell_placed']} tp={diag['tp_placed']} "
                f"kept_buy={diag['buy_kept']} kept_sell={diag['sell_kept']} "
                f"skip_bal={diag['skipped_balance']} cancels={diag['cancels']} | "
                f"realized={diag['sum_realized_pnl']:+.0f} unrealized={diag['sum_unrealized_pnl']:+.0f} "
                f"instr={len(response.instructions)} resp_ms={elapsed*1000:.0f}"
            )
        return response


if __name__ == "__main__":
    """
    Standalone test against the local proxy. From inside `agents/`:
      python DirectionalMomentumAgent.py --port 8888 --agent_id 0 \
          --params target_max_base_per_book=1.0 chunk_qty=0.2 obi_weight=0.5 \
                   mom_weight=0.5 mom_norm_bps=15 signal_threshold=0.15 \
                   signal_power=1.5 alpha_smooth_alpha=0.4 \
                   entry_aggressive_alpha=0.8 entry_moderate_alpha=0.5 \
                   take_profit_alpha_thresh=0.10 exit_offset_bps=8 \
                   stop_loss_pct=0.01 expiry_seconds=30 diag_every=20
    """
    launch(DirectionalMomentumAgent)
