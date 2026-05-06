# SPDX-License-Identifier: MIT
"""
AdaptiveMarketMakerAgent

Inventory-managed limit-order market maker with microprice fair-value, volatility-adaptive
spread, and per-book stop-loss. Designed against the SN-79 (taos.im) reward function:

  final_score = kappa_weight * kappa_score + pnl_weight * pnl_score
  kappa_score = median_over_books( activity * pnl * normalized_kappa_3 ) - outlier_penalty
  kappa_3     = (mu - tau) / [LPM_3(tau)]^(1/3)  on REALIZED round-trip P&L

Design priorities (in order):
  1. Trade actively on EVERY book (silent books decay to 0 and tank the median).
  2. Realize round-trip P&L (Kappa is fed by closed-trade returns only).
  3. Cap downside (LPM_3 in denominator -> realized losses are punished cubically).
  4. Volume in [50%, 70%] of cap (max activity boost, no lockout risk).
  5. Sub-200 ms response (validator applies exp(5 t/timeout) delay penalty).
  6. Identical logic across books (IQR outlier penalty crushes specialization).

Mechanics:
  - Fair value = microprice = (bid_qty*ask + ask_qty*bid) / (bid_qty + ask_qty)
  - Reservation price r = micro - gamma * inventory * sigma^2  (Avellaneda-Stoikov skew)
  - Half-spread     h = base_half_spread + gamma * sigma^2
  - Quote postOnly GTT buy at r-h and sell at r+h; expiry auto-cleans stale quotes
  - Re-quote only when target moves more than `requote_tolerance_pct` of mid
  - onTrade: track per-book inventory + realized P&L; fire stop-loss market flatten
    when cumulative loss on this book exceeds `stop_loss_pct` of per-book wealth.
  - Volume gate: when account.traded_volume on a book exceeds
    `vol_pause_pct` of the per-book cap, pull all quotes until window decays.

Strategy parameters (pass via --agent.params on the miner command line):
  base_qty=0.5            base order size in BASE per side
  base_half_spread=0.0008 fraction of mid added each side; 8 bps default
  gamma=8.0               inventory aversion + vol-driven spread widening
  vol_window=64           EWMA samples for sigma estimation
  inventory_cap_pct=0.10  hard cap on |inventory| as fraction of per-book wealth
  stop_loss_pct=0.005     close inventory when realized+unrealized loss > 0.5% of capital
  vol_pause_pct=0.70      pull quotes when traded_volume > 70% of cap
  expiry_seconds=30       GTT expiry of resting quotes (sim seconds). MUST exceed
                          validator publish_interval (~5s) plus set_delays overhead.
  max_half_spread_pct=0.005   ceiling on half-spread (0.5% of mid). Prevents EWMA
                          blowups on noisy books from quoting 20%+ wide and never
                          filling. Total quoted spread <= 2 * max_half_spread_pct.
  requote_tolerance_pct=0.0015  re-quote only if target shifts > 15 bps. Heavily
                          influences instruction count per response: 3 bps caused
                          ~768 instr/response -> 4.5s decompression on validator.
  capital_turnover_cap=1.0      mirror of validator scoring.activity.capital_turnover_cap

The defaults assume the live SN-79 simulation starting capital and decimal precision
exposed via state.config. They are conservative; tune base_qty / gamma after observing
fills + Kappa on testnet.
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
# Per-book state container
# ---------------------------------------------------------------------------
# NOTE: We deliberately do NOT use @dataclass here. The miner base class loads
# this module via importlib.util.spec_from_file_location + exec_module without
# registering the module in sys.modules first (taos/common/neurons/miner.py:90-92).
# When @dataclass runs during exec it tries sys.modules[cls.__module__].__dict__,
# which yields None.__dict__ -> AttributeError. Plain classes side-step that
# entirely. This matches the pattern used by all shipped sample agents.
class BookState:
    """Rolling per-book state that survives across state updates within a simulation."""
    __slots__ = (
        "inventory",         # Net BASE position from our trades since last flatten
        "avg_cost",          # Volume-weighted average price of current inventory
        "cum_realized_pnl",  # Realized P&L on this book since simulation start (QUOTE)
        "last_mid",          # Last midprice observed (for volatility EWMA)
        "sigma2",            # EWMA variance of log-returns
        "last_buy_price",    # Most recent target buy price we quoted
        "last_sell_price",   # Most recent target sell price we quoted
        "paused_until_ts",   # Volume gate: pause new quotes until this sim time
        "stop_armed",        # Disable stop-loss briefly right after triggering
        "ewma_short",        # Short-window EWMA of mid (trend detection)
        "ewma_long",         # Long-window EWMA of mid (trend detection)
    )

    def __init__(self) -> None:
        self.inventory = 0.0
        self.avg_cost = 0.0
        self.cum_realized_pnl = 0.0
        self.last_mid = 0.0
        self.sigma2 = 0.0
        self.last_buy_price = 0.0
        self.last_sell_price = 0.0
        self.paused_until_ts = 0
        self.stop_armed = True
        self.ewma_short = 0.0
        self.ewma_long = 0.0


class AdaptiveMarketMakerAgent(FinanceSimulationAgent):
    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def initialize(self):
        c = self.config
        # Strategy parameters (all overridable via --agent.params)
        # Per-fill size in BASE units. Smaller = less per-fill risk, more granular
        # inventory control. With per_book_wealth ~$390 and mid ~$300, inv cap is
        # ~0.65 BASE/book; base_qty=0.5 exhausted 77% of the cap on a single fill.
        # 0.2 BASE = ~31% of cap per fill, so we get 3 fills before hitting the cap
        # rather than 1. Smaller fills also mean smaller adverse-selection losses.
        self.base_qty: float = float(getattr(c, "base_qty", 0.2))
        self.base_half_spread: float = float(getattr(c, "base_half_spread", 0.0008))
        # gamma drives Avellaneda-Stoikov inventory skew AND vol-driven spread widening.
        # gamma=20 was too aggressive: on noisy books, sigma^2 EWMA spikes blew the
        # quoted spread out to 20-35% of mid (we observed bids @ 181 / asks @ 383 on
        # a 290 mid). gamma=8 keeps spreads workable while still skewing for inventory.
        self.gamma: float = float(getattr(c, "gamma", 8.0))
        self.vol_window: int = int(getattr(c, "vol_window", 64))
        # Inventory cap as fraction of per-book wealth. Tighter cap = less max
        # accumulation risk in trending markets. 0.20 lets us absorb a few fills
        # before the gate fires. Combined with active-flattening below, this
        # keeps |inventory| bounded even in one-way markets.
        self.inventory_cap_pct: float = float(getattr(c, "inventory_cap_pct", 0.20))
        # When |inventory| exceeds inv_cap * `flatten_threshold_mult`, we switch
        # the offsetting side to AGGRESSIVE (top-of-queue) regardless of edge.
        # This actually closes the position instead of just refusing to add to it.
        # Without this, in a trend we accumulate inventory, hit the cap, refuse
        # to add (correctly) but then sit indefinitely with the toxic position.
        self.flatten_threshold_mult: float = float(getattr(c, "flatten_threshold_mult", 1.0))
        self.stop_loss_pct: float = float(getattr(c, "stop_loss_pct", 0.005))
        self.vol_pause_pct: float = float(getattr(c, "vol_pause_pct", 0.70))
        # GTT expiry in simulation seconds. The original expiry=30 showed
        # kept=78 in production (orders persisted across cycles). Switching to
        # GTC was tried but GTC orders never appeared in account.orders the
        # next cycle in this simulator — it does not reflect GTC orders back
        # in state snapshots. Back to GTT with 3600 sim-sec so orders survive
        # many cycles while still expiring if the miner dies.
        self.expiry_seconds: int = int(getattr(c, "expiry_seconds", 3600))
        # Hard cap on half-spread as a fraction of mid. Quotes wider than this on
        # volatile books simply never get filled, so we'd rather under-protect a
        # bit and actually trade than quote at 25% wide and earn zero RT volume.
        self.max_half_spread_pct: float = float(getattr(c, "max_half_spread_pct", 0.005))
        # Re-quote only when target moves more than this fraction of mid. 3 bps
        # was too tight (we re-quoted on every cycle on every book -> 768 instr
        # per response -> 4.5s decompression -> max latency penalty). 15 bps lets
        # quotes rest passively and slashes wire payload.
        self.requote_tolerance_pct: float = float(getattr(c, "requote_tolerance_pct", 0.0015))
        # Minimum required edge (distance from mid) for any quote, in basis points.
        # If the top-of-queue price gives less edge than this, we either SKIP or
        # quote DEEPER (passive at the edge level), depending on `min_edge_mode`.
        # Sequence: 10 bps was way too tight (only ~2% of books satisfied it). 3 bps
        # was still too tight: the live simulator runs many books with full spread
        # under 6 bps, so the "deeper" fallback placed orders BELOW the bid and
        # nothing filled (kept=0 for 23 minutes). 0.5 bps lets the at-touch
        # branches fire on essentially every book; the inventory cap, active
        # flatten, and trend gate handle adverse selection downstream.
        self.min_edge_bps: float = float(getattr(c, "min_edge_bps", 0.5))
        # What to do when a side fails the edge gate at top-of-queue:
        #   "skip"   -> don't quote that side this cycle (lowest activity, lowest AS)
        #   "deeper" -> still quote, but at mid +/- min_edge (behind the touch).
        #               Order may fill less often but each fill has guaranteed edge.
        self.min_edge_mode: str = str(getattr(c, "min_edge_mode", "deeper")).lower()
        # Trend-detection circuit breaker. The single biggest cause of toxic
        # accumulation is one-sided fills during a price trend: market grinds up,
        # our deeper sell quotes get picked off as it crosses through them, our
        # buy quotes sit below the rising market and never fill. We track a
        # short EWMA vs a long EWMA of mid; when they diverge by more than
        # `trend_pause_bps`, we PAUSE the side that the trend is hitting:
        #   - Up trend (short_ewma > long_ewma * (1 + thresh)) -> pause sells.
        #   - Down trend (short_ewma < long_ewma * (1 - thresh)) -> pause buys.
        # Active flatten still runs (we still close excess inventory urgently),
        # but we don't add NEW exposure into a trend.
        self.trend_pause_bps: float = float(getattr(c, "trend_pause_bps", 8.0))
        self.trend_ewma_short_alpha: float = float(getattr(c, "trend_ewma_short_alpha", 0.3))
        self.trend_ewma_long_alpha: float = float(getattr(c, "trend_ewma_long_alpha", 0.05))
        self.capital_turnover_cap: float = float(getattr(c, "capital_turnover_cap", 1.0))

        # Per-book persistent state. Reset on simulation start.
        self.book_state: Dict[int, BookState] = {}
        # Cycle counter for periodic diagnostic logging.
        self._cycle = 0
        self._diag_every = int(getattr(c, "diag_every", 20))

        bt.logging.info(
            "AdaptiveMarketMakerAgent initialized | "
            f"base_qty={self.base_qty} half_spread={self.base_half_spread} "
            f"max_half_spread_pct={self.max_half_spread_pct} gamma={self.gamma} "
            f"inv_cap_pct={self.inventory_cap_pct} stop_loss_pct={self.stop_loss_pct} "
            f"vol_pause_pct={self.vol_pause_pct} expiry_s={self.expiry_seconds} "
            f"requote_tol={self.requote_tolerance_pct} diag_every={self._diag_every}"
        )

    # ------------------------------------------------------------------
    # Event hooks
    # ------------------------------------------------------------------
    def onStart(self, event: SimulationStartEvent) -> None:
        """Reset per-book state at the start of a fresh simulation."""
        bt.logging.info("Simulation start: clearing per-book state")
        self.book_state.clear()

    def onTrade(self, event: TradeEvent, validator: str = None) -> None:
        """
        Track inventory + realized P&L on each fill, and arm the per-book stop-loss.

        We are either the maker or the taker; the trade event carries the side from the
        taker perspective. Determine our role and direction, then update inventory.
        Inventory is BASE units we hold from our own trades since last flatten.
        """
        bs = self.book_state.setdefault(event.bookId, BookState())

        is_taker = (event.takerAgentId == self.uid)
        is_maker = (event.makerAgentId == self.uid)
        if not (is_taker or is_maker):
            return

        # Determine our trade direction (BUY = we acquired BASE; SELL = we sold BASE).
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

        # Realized P&L only crystallizes when a trade reduces |inventory|.
        # If signs flip or shrink, the smaller of the two magnitudes is the closed leg.
        if prev_inv != 0.0 and (new_inv == 0.0 or (prev_inv * new_inv) < 0.0 or abs(new_inv) < abs(prev_inv)):
            closed = min(abs(prev_inv), qty)  # quantity that actually closed
            sign = 1.0 if prev_inv > 0 else -1.0
            # P&L per closed unit = (exit - entry) * sign
            pnl = (px - bs.avg_cost) * sign * closed
            bs.cum_realized_pnl += pnl

        # Update average cost: weighted average if same direction, reset if flipped.
        if (prev_inv == 0.0) or (prev_inv * signed_qty > 0):
            total_qty = abs(prev_inv) + qty
            if total_qty > 0:
                bs.avg_cost = (abs(prev_inv) * bs.avg_cost + qty * px) / total_qty
        elif (prev_inv * new_inv) < 0:
            # Direction flipped: the residual is fresh inventory at this price
            bs.avg_cost = px
        elif new_inv == 0.0:
            bs.avg_cost = 0.0

        bs.inventory = new_inv

    def onOrderRejected(self, event) -> None:
        """Surface order rejections at warning level (insufficient balance, max orders, etc.)."""
        super().onOrderRejected(event)
        bt.logging.warning(
            f"BOOK {getattr(event, 'bookId', '?')} order rejected: {getattr(event, 'message', '')}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _miner_wealth_per_book(self) -> float:
        """Approximate per-book initial capital in QUOTE."""
        cfg = self.simulation_config
        wealth = float(getattr(cfg, "miner_wealth", 0.0)) or 0.0
        n_books = max(1, int(getattr(cfg, "book_count", 1)))
        return wealth / n_books if wealth > 0 else 0.0

    def _volume_cap_per_book(self) -> float:
        """Validator-side cap = capital_turnover_cap * miner_wealth (assessed per book)."""
        return self._miner_wealth_per_book() * self.capital_turnover_cap

    def _round_price(self, p: float) -> float:
        return round(p, self.simulation_config.priceDecimals)

    def _round_qty(self, q: float) -> float:
        return round(q, self.simulation_config.volumeDecimals)

    def _update_volatility(self, bs: BookState, mid: float) -> None:
        """EWMA log-return variance with span = self.vol_window."""
        if bs.last_mid > 0 and mid > 0:
            r = math.log(mid / bs.last_mid)
            # EWMA with alpha = 2/(N+1)
            alpha = 2.0 / (self.vol_window + 1)
            bs.sigma2 = (1 - alpha) * bs.sigma2 + alpha * r * r
        bs.last_mid = mid
        # Trend detection: short and long EWMAs of mid. Initialize to mid on
        # the first cycle so the first trend reading isn't garbage.
        if bs.ewma_short <= 0:
            bs.ewma_short = mid
            bs.ewma_long = mid
        else:
            bs.ewma_short = (1 - self.trend_ewma_short_alpha) * bs.ewma_short + self.trend_ewma_short_alpha * mid
            bs.ewma_long = (1 - self.trend_ewma_long_alpha) * bs.ewma_long + self.trend_ewma_long_alpha * mid

    def _maybe_stop_loss(
        self,
        response: FinanceAgentResponse,
        book_id: int,
        bs: BookState,
        mid: float,
    ) -> bool:
        """
        Returns True if a stop-loss flatten was triggered (caller should skip quoting this book).
        Flatten only the inventory we currently hold via a market order.
        """
        if bs.inventory == 0.0 or not bs.stop_armed:
            return False
        per_book_cap = self._miner_wealth_per_book()
        if per_book_cap <= 0:
            return False

        # Mark-to-market loss in QUOTE = (cost_basis - mark) * sign(inventory) * |inventory|
        unrealized = (mid - bs.avg_cost) * (1.0 if bs.inventory > 0 else -1.0) * abs(bs.inventory)
        loss_threshold = -self.stop_loss_pct * per_book_cap
        if unrealized < loss_threshold:
            qty = self._round_qty(abs(bs.inventory))
            if qty > 0:
                direction = OrderDirection.SELL if bs.inventory > 0 else OrderDirection.BUY
                response.market_order(
                    book_id=book_id,
                    direction=direction,
                    quantity=qty,
                )
                bt.logging.warning(
                    f"BOOK {book_id} STOP-LOSS firing: inv={bs.inventory:+.4f} avg_cost={bs.avg_cost:.4f} "
                    f"mid={mid:.4f} unrealized={unrealized:.4f} (< {loss_threshold:.4f})"
                )
                bs.stop_armed = False  # disarm one cycle to avoid double-flatten
                return True
        else:
            bs.stop_armed = True
        return False

    def _is_volume_gated(self, account, per_book_cap: float) -> bool:
        traded = float(getattr(account, "traded_volume", 0.0) or 0.0)
        if per_book_cap <= 0:
            return False
        return traded >= self.vol_pause_pct * per_book_cap

    # ------------------------------------------------------------------
    # Main strategy
    # ------------------------------------------------------------------
    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        t0 = time.time()
        response = FinanceAgentResponse(agent_id=self.uid)

        per_book_cap = self._volume_cap_per_book()
        per_book_wealth = self._miner_wealth_per_book()
        priceDecimals = self.simulation_config.priceDecimals
        # Back to GTT: empirically the original GTT expiry=30 showed kept=78
        # (orders persisting across cycles). GTC orders never appeared in
        # account.orders next cycle — this specific simulator doesn't reflect
        # GTC orders back in state snapshots. 3600 sim-sec ≈ effectively
        # infinite at any reasonable sim speed; still lets the engine expire
        # long-abandoned quotes cleanly.
        expiry_ns = self.expiry_seconds * 1_000_000_000
        self._cycle += 1
        # Per-cycle counters for diagnostic logging at end of respond().
        diag = {
            "books": 0, "stop_loss": 0, "vol_gated": 0,
            "buy_quoted": 0, "sell_quoted": 0,
            "buy_skipped_invcap": 0, "sell_skipped_invcap": 0,
            "buy_skipped_balance": 0, "sell_skipped_balance": 0,
            "buy_skipped_edge": 0, "sell_skipped_edge": 0,
            "cancels": 0, "kept_buy": 0, "kept_sell": 0,
            "inv_long_books": 0, "inv_short_books": 0, "inv_flat_books": 0,
            "sum_inv_base": 0.0,
            # Competitiveness: at-top vs deeper-passive.
            "buy_inside": 0, "sell_inside": 0,
            "buy_deeper": 0, "sell_deeper": 0,
            # Active flatten: # books where we forced top-of-queue to close excess inv.
            "urgent_flatten_buy": 0, "urgent_flatten_sell": 0,
            # Trend gate: # books where we suppressed a side because of a trend.
            "trend_paused_buy": 0, "trend_paused_sell": 0,
            "sum_traded_volume": 0.0,
            # GTC order-persistence probe: total existing orders seen this cycle
            # across all books. Should be > 0 after cycle 1 if GTC orders are
            # being reflected in state snapshots. Stays 0 → GTC not persisting.
            "existing_buy_orders": 0, "existing_sell_orders": 0,
        }

        for book_id, book in state.books.items():
            if not book.bids or not book.asks:
                continue

            bs = self.book_state.setdefault(book_id, BookState())
            account = self.accounts.get(book_id)
            if account is None:
                continue

            # ----- 1. Fair value via microprice -----
            best_bid = book.bids[0]
            best_ask = book.asks[0]
            bid, bid_qty = float(best_bid.price), float(best_bid.quantity)
            ask, ask_qty = float(best_ask.price), float(best_ask.quantity)
            tick = 10 ** (-priceDecimals)
            if ask <= bid:
                ask = bid + tick  # safety against weird snapshots

            qsum = bid_qty + ask_qty
            micro = (bid_qty * ask + ask_qty * bid) / qsum if qsum > 0 else 0.5 * (bid + ask)
            mid = 0.5 * (bid + ask)

            self._update_volatility(bs, mid)

            diag["books"] += 1
            if bs.inventory > 1e-9:
                diag["inv_long_books"] += 1
            elif bs.inventory < -1e-9:
                diag["inv_short_books"] += 1
            else:
                diag["inv_flat_books"] += 1
            diag["sum_inv_base"] += bs.inventory
            diag["sum_traded_volume"] += float(getattr(account, "traded_volume", 0.0) or 0.0)

            # ----- 2. Stop-loss gate -----
            if self._maybe_stop_loss(response, book_id, bs, mid):
                # Cancel resting quotes on the same book; we are flattening this cycle.
                rest_ids = [o.id for o in account.orders] if account.orders else []
                if rest_ids:
                    response.cancel_orders(book_id=book_id, order_ids=rest_ids)
                    diag["cancels"] += len(rest_ids)
                diag["stop_loss"] += 1
                continue

            # ----- 3. Volume cap gate -----
            if self._is_volume_gated(account, per_book_cap):
                # Already at >= vol_pause_pct of cap. Pull quotes; do not place new ones.
                rest_ids = [o.id for o in account.orders] if account.orders else []
                if rest_ids:
                    response.cancel_orders(book_id=book_id, order_ids=rest_ids)
                    diag["cancels"] += len(rest_ids)
                diag["vol_gated"] += 1
                continue

            # ----- 4. Avellaneda-Stoikov style reservation + spread -----
            sigma2 = bs.sigma2
            # Inventory skew in absolute price units (positive when long, so it
            # subtracts from reservation -> quotes shift DOWN -> we sell more
            # eagerly and buy more passively, encouraging flattening).
            inv_skew_price = self.gamma * bs.inventory * sigma2 * mid
            reservation = micro - inv_skew_price
            half_spread = self.base_half_spread * mid + self.gamma * sigma2 * mid
            half_spread = max(half_spread, 2 * tick)
            half_spread = min(half_spread, self.max_half_spread_pct * mid)

            # Model's "raw" preferred prices (already include inventory skew).
            raw_buy = reservation - half_spread
            raw_sell = reservation + half_spread

            # Top-of-queue prices and min-edge floor.
            has_room = (ask - bid) > (2 * tick)
            top_buy = (bid + tick) if has_room else bid
            top_sell = (ask - tick) if has_room else ask
            min_edge = self.min_edge_bps * 1e-4 * mid

            # ----- 5. Active flatten when inventory blew past the cap -----
            # If |inventory| > flatten_threshold_mult * inv_cap, we OVERRIDE the
            # opposing side to top-of-queue regardless of edge so we actually
            # close the position. Without this, "skip when at cap" leaves us
            # holding a toxic position indefinitely after a trend.
            inv_cap_units = (self.inventory_cap_pct * per_book_wealth / mid) if mid > 0 else float("inf")
            flatten_threshold = self.flatten_threshold_mult * inv_cap_units
            urgent_flatten_buy = bs.inventory < -flatten_threshold  # heavily short -> aggressive buy
            urgent_flatten_sell = bs.inventory > flatten_threshold  # heavily long  -> aggressive sell
            if urgent_flatten_buy:
                diag["urgent_flatten_buy"] += 1
            if urgent_flatten_sell:
                diag["urgent_flatten_sell"] += 1

            # ----- 5b. Trend-gate: don't add NEW exposure into a strong trend.
            # Active flatten still runs above so toxic positions still get closed.
            # The gate only suppresses the side that's getting hit by the trend.
            trend_thresh = self.trend_pause_bps * 1e-4
            if bs.ewma_long > 0:
                trend_ratio = (bs.ewma_short / bs.ewma_long) - 1.0
            else:
                trend_ratio = 0.0
            up_trend = trend_ratio > trend_thresh
            down_trend = trend_ratio < -trend_thresh
            # Only pause adding to an ALREADY-ADVERSE position in a trend.
            # NEVER pause a flat book — we need fills to build any position.
            #
            # OLD (broken): `inventory <= 0` fired for flat (0) too, blocking
            # both sides on ~60% of books when we had no inventory at all.
            # At c=980: skip_invcap=77 buy + 27 sell = 104/128 books silently
            # blocked, yet flat=128. The trend gate was the primary blocker.
            #
            # NEW: only gates when we already hold inventory in the wrong
            # direction. Flat books always quote both sides. Adverse positions
            # still get gated so we don't double down, and the active-flatten
            # path still closes them aggressively.
            trend_pause_sell = up_trend and bs.inventory < -1e-9   # SHORT in up-trend
            trend_pause_buy = down_trend and bs.inventory > 1e-9    # LONG in down-trend
            if trend_pause_buy:
                diag["trend_paused_buy"] += 1
            if trend_pause_sell:
                diag["trend_paused_sell"] += 1

            # ----- 6. Resolve target_buy -----
            # Decision tree:
            #  a) Urgent flatten short -> top_buy at touch (any edge, just close it).
            #  b) Model wants aggressive (raw_buy >= top_buy AND top_buy has edge) -> top_buy.
            #  c) Bid level has edge -> bid (join the queue, in line for a fill).
            #  d) Otherwise: "deeper" mode picks `mid - min_edge` (with skew applied);
            #     "skip" mode skips the side this cycle.
            quote_buy_edge = True
            buy_at_top_quote = False
            if urgent_flatten_buy:
                target_buy = top_buy
                buy_at_top_quote = True
            elif raw_buy >= top_buy and (mid - top_buy) >= min_edge:
                target_buy = top_buy
                buy_at_top_quote = True
            elif (mid - bid) >= min_edge:
                # Quote at the more conservative of (bid, raw_buy clipped to bid+tick).
                target_buy = max(min(raw_buy, top_buy), bid)
                buy_at_top_quote = (target_buy >= top_buy - 0.5 * tick)
            elif self.min_edge_mode == "deeper":
                # Apply skew to the deeper level so a heavily-short book still
                # quotes its buy more aggressively than a flat book.
                target_buy = mid - min_edge - inv_skew_price
                # Must not cross the ask (post-only would be rejected).
                if target_buy >= ask - 0.5 * tick:
                    target_buy = ask - tick  # take top-of-queue when crossed
                    buy_at_top_quote = True
                # CRITICAL: never sit BEHIND the touch. If the market is tighter
                # than min_edge, the calculated deeper price is below `bid`,
                # which means our order rests deeper in the book and never
                # gets filled (this was the "kept=0 forever" bug). Always join
                # the queue at top-of-bid in that case.
                elif target_buy < bid:
                    target_buy = bid
                    buy_at_top_quote = True
            else:
                quote_buy_edge = False

            # ----- 7. Resolve target_sell (mirror of buy logic) -----
            quote_sell_edge = True
            sell_at_top_quote = False
            if urgent_flatten_sell:
                target_sell = top_sell
                sell_at_top_quote = True
            elif raw_sell <= top_sell and (top_sell - mid) >= min_edge:
                target_sell = top_sell
                sell_at_top_quote = True
            elif (ask - mid) >= min_edge:
                target_sell = min(max(raw_sell, top_sell), ask)
                sell_at_top_quote = (target_sell <= top_sell + 0.5 * tick)
            elif self.min_edge_mode == "deeper":
                target_sell = mid + min_edge - inv_skew_price
                if target_sell <= bid + 0.5 * tick:
                    target_sell = bid + tick
                    sell_at_top_quote = True
                # CRITICAL mirror of buy-side fix: never sit ABOVE the touch
                # in tight markets. If calculated deeper price > ask, our sell
                # rests deeper in the book and never fills. Sit AT the ask
                # instead so we're at the top of the sell queue.
                elif target_sell > ask:
                    target_sell = ask
                    sell_at_top_quote = True
            else:
                quote_sell_edge = False

            # ----- 8. Round, clamp, and ensure non-crossing -----
            if quote_buy_edge:
                target_buy = self._round_price(target_buy)
                target_buy = min(target_buy, self._round_price(ask - tick))
            if quote_sell_edge:
                target_sell = self._round_price(target_sell)
                target_sell = max(target_sell, self._round_price(bid + tick))
            if quote_buy_edge and quote_sell_edge and target_sell <= target_buy:
                target_sell = self._round_price(target_buy + tick)

            edge_buy_ok = quote_buy_edge
            edge_sell_ok = quote_sell_edge

            # Competitiveness telemetry — track where each quote sat:
            #   skip_edge = side not quoted at all (no edge available, mode=skip)
            #   inside    = at top-of-queue with edge (best/inside the spread)
            #   deeper    = behind the touch with edge (mode=deeper fallback)
            if not edge_buy_ok:
                diag["buy_skipped_edge"] += 1
            elif buy_at_top_quote:
                diag["buy_inside"] += 1
            else:
                diag["buy_deeper"] += 1
            if not edge_sell_ok:
                diag["sell_skipped_edge"] += 1
            elif sell_at_top_quote:
                diag["sell_inside"] += 1
            else:
                diag["sell_deeper"] += 1

            # ----- 9. Inventory cap, edge gate, and trend gate combine -----
            # Note: inv_cap_units was computed in step 5 above. We re-use it here so
            # the cap and active-flatten thresholds stay in lockstep. Trend gate
            # only blocks ADDING to exposure into a trend; if urgent_flatten is
            # firing, that side bypasses the trend gate so we can still close.
            quote_buy = (bs.inventory < inv_cap_units) and edge_buy_ok
            quote_sell = (bs.inventory > -inv_cap_units) and edge_sell_ok
            if trend_pause_buy and not urgent_flatten_buy:
                quote_buy = False
            if trend_pause_sell and not urgent_flatten_sell:
                quote_sell = False

            # Bigger size when actively flattening so we close faster. Cap at 2x
            # base_qty to avoid blowing through balance.
            base_qty_eff = self.base_qty
            if urgent_flatten_buy and quote_buy:
                base_qty_eff = min(self.base_qty * 2.0, abs(bs.inventory))
            elif urgent_flatten_sell and quote_sell:
                base_qty_eff = min(self.base_qty * 2.0, abs(bs.inventory))
            qty = self._round_qty(base_qty_eff)
            if qty <= 0:
                continue

            # ----- 10. Smart re-quote: only cancel/replace orders that have moved enough -----
            tol = self.requote_tolerance_pct * mid
            existing_buys = [o for o in account.orders if int(o.side) == int(OrderDirection.BUY)]
            existing_sells = [o for o in account.orders if int(o.side) == int(OrderDirection.SELL)]
            diag["existing_buy_orders"] += len(existing_buys)
            diag["existing_sell_orders"] += len(existing_sells)

            # Cancel buys that are stale or wrong-priced.
            stale_buy_ids = []
            keep_buy = False
            for o in existing_buys:
                if abs(float(o.price) - target_buy) > tol or not quote_buy:
                    stale_buy_ids.append(o.id)
                else:
                    keep_buy = True
            stale_sell_ids = []
            keep_sell = False
            for o in existing_sells:
                if abs(float(o.price) - target_sell) > tol or not quote_sell:
                    stale_sell_ids.append(o.id)
                else:
                    keep_sell = True

            if stale_buy_ids or stale_sell_ids:
                response.cancel_orders(book_id=book_id, order_ids=stale_buy_ids + stale_sell_ids)
                diag["cancels"] += len(stale_buy_ids) + len(stale_sell_ids)
            if keep_buy:
                diag["kept_buy"] += 1
            if keep_sell:
                diag["kept_sell"] += 1

            # ----- 8. Place new buy quote -----
            if not quote_buy:
                # Only attribute the skip to inventory cap if the edge gate already
                # allowed it (otherwise buy_skipped_edge has already been counted).
                if edge_buy_ok:
                    diag["buy_skipped_invcap"] += 1
            elif not keep_buy:
                cost = qty * target_buy
                if account.quote_balance.free >= cost:
                    response.limit_order(
                        book_id=book_id,
                        direction=OrderDirection.BUY,
                        quantity=qty,
                        price=target_buy,
                        postOnly=True,
                        timeInForce=TimeInForce.GTT,
                        expiryPeriod=expiry_ns,
                        stp=STP.CANCEL_OLDEST,
                    )
                    diag["buy_quoted"] += 1
                else:
                    diag["buy_skipped_balance"] += 1

            # ----- 9. Place new sell quote -----
            if not quote_sell:
                if edge_sell_ok:
                    diag["sell_skipped_invcap"] += 1
            elif not keep_sell:
                if account.base_balance.free >= qty:
                    response.limit_order(
                        book_id=book_id,
                        direction=OrderDirection.SELL,
                        quantity=qty,
                        price=target_sell,
                        postOnly=True,
                        timeInForce=TimeInForce.GTT,
                        expiryPeriod=expiry_ns,
                        stp=STP.CANCEL_OLDEST,
                    )
                    diag["sell_quoted"] += 1
                else:
                    diag["sell_skipped_balance"] += 1

            # Track last quoted prices only when we actually quoted that side; using
            # a sentinel target value when not quoting could mislead later cycles.
            if quote_buy:
                bs.last_buy_price = target_buy
            if quote_sell:
                bs.last_sell_price = target_sell

        elapsed = time.time() - t0
        if elapsed > 0.25:
            bt.logging.warning(
                f"AdaptiveMarketMaker respond() took {elapsed:.3f}s "
                f"(books={len(state.books)}, instructions={len(response.instructions)})"
            )
        else:
            bt.logging.debug(
                f"AdaptiveMarketMaker respond() {elapsed*1000:.1f}ms "
                f"books={len(state.books)} instructions={len(response.instructions)}"
            )

        # Periodic diagnostic. Tells us at a glance whether we're actually quoting,
        # which gate is killing us, what our local inventory tracker thinks, and how
        # competitive our quotes are vs the existing touch.
        if self._cycle % self._diag_every == 0:
            mw = float(getattr(self.simulation_config, "miner_wealth", 0.0))
            bc = int(getattr(self.simulation_config, "book_count", 1))
            bt.logging.info(
                f"DIAG c={self._cycle} miner_wealth={mw:.0f} book_count={bc} "
                f"per_book_wealth={per_book_wealth:.2f} per_book_cap={per_book_cap:.2f} | "
                f"books={diag['books']} stop_loss={diag['stop_loss']} vol_gated={diag['vol_gated']} | "
                f"buy: quoted={diag['buy_quoted']} kept={diag['kept_buy']} "
                f"inside={diag['buy_inside']} deeper={diag['buy_deeper']} "
                f"skip_edge={diag['buy_skipped_edge']} skip_invcap={diag['buy_skipped_invcap']} "
                f"skip_bal={diag['buy_skipped_balance']} | "
                f"sell: quoted={diag['sell_quoted']} kept={diag['kept_sell']} "
                f"inside={diag['sell_inside']} deeper={diag['sell_deeper']} "
                f"skip_edge={diag['sell_skipped_edge']} skip_invcap={diag['sell_skipped_invcap']} "
                f"skip_bal={diag['sell_skipped_balance']} | "
                f"inv: long={diag['inv_long_books']} short={diag['inv_short_books']} "
                f"flat={diag['inv_flat_books']} sum_inv_base={diag['sum_inv_base']:+.2f} "
                f"flatten_buy={diag['urgent_flatten_buy']} flatten_sell={diag['urgent_flatten_sell']} "
                f"trend_pause_buy={diag['trend_paused_buy']} trend_pause_sell={diag['trend_paused_sell']} | "
                f"traded_vol={diag['sum_traded_volume']:.0f} cancels={diag['cancels']} "
                f"existing_buy={diag['existing_buy_orders']} existing_sell={diag['existing_sell_orders']} "
                f"instr={len(response.instructions)} resp_ms={elapsed*1000:.0f}"
            )
        return response


if __name__ == "__main__":
    """
    Standalone test against the local proxy. From inside `agents/`:
      python AdaptiveMarketMakerAgent.py --port 8888 --agent_id 0 \
          --params base_qty=0.5 base_half_spread=0.0008 gamma=8.0 \
                   stop_loss_pct=0.005 vol_pause_pct=0.70 expiry_seconds=30 \
                   max_half_spread_pct=0.005 requote_tolerance_pct=0.0015
    """
    launch(AdaptiveMarketMakerAgent)
