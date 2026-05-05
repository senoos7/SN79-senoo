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


class AdaptiveMarketMakerAgent(FinanceSimulationAgent):
    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def initialize(self):
        c = self.config
        # Strategy parameters (all overridable via --agent.params)
        self.base_qty: float = float(getattr(c, "base_qty", 0.5))
        self.base_half_spread: float = float(getattr(c, "base_half_spread", 0.0008))
        # gamma drives Avellaneda-Stoikov inventory skew AND vol-driven spread widening.
        # gamma=20 was too aggressive: on noisy books, sigma^2 EWMA spikes blew the
        # quoted spread out to 20-35% of mid (we observed bids @ 181 / asks @ 383 on
        # a 290 mid). gamma=8 keeps spreads workable while still skewing for inventory.
        self.gamma: float = float(getattr(c, "gamma", 8.0))
        self.vol_window: int = int(getattr(c, "vol_window", 64))
        # Inventory cap as fraction of per-book wealth. 0.10 was too tight: with
        # base_qty=0.5 BASE per fill on a small per-book wealth, a single fill
        # exceeded the cap, locking us into flatten-only mode on that side. Once
        # the offsetting fill stalls (queue position, market drift), volume goes
        # to zero. 0.50 lets ~5-10 fills stack before the gate kicks in.
        self.inventory_cap_pct: float = float(getattr(c, "inventory_cap_pct", 0.50))
        self.stop_loss_pct: float = float(getattr(c, "stop_loss_pct", 0.005))
        self.vol_pause_pct: float = float(getattr(c, "vol_pause_pct", 0.70))
        self.expiry_seconds: int = int(getattr(c, "expiry_seconds", 30))
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
        # Empirically 10 bps was way too tight (only ~2% of books had spread
        # wide enough to satisfy it -> we sat out the simulator). 3 bps lets us
        # participate on most books while still defending against the worst
        # adverse-selection at zero-edge front-of-queue quoting.
        self.min_edge_bps: float = float(getattr(c, "min_edge_bps", 3.0))
        # What to do when a side fails the edge gate at top-of-queue:
        #   "skip"   -> don't quote that side this cycle (lowest activity, lowest AS)
        #   "deeper" -> still quote, but at mid +/- min_edge (behind the touch).
        #               Order may fill less often but each fill has guaranteed edge.
        self.min_edge_mode: str = str(getattr(c, "min_edge_mode", "deeper")).lower()
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
        # GTT expiry in simulation nanoseconds.
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
            "sum_traded_volume": 0.0,
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
            inv_skew = self.gamma * bs.inventory * sigma2
            reservation = micro - inv_skew * mid  # sigma2 is dimensionless; scale by mid
            half_spread = self.base_half_spread * mid + self.gamma * sigma2 * mid
            # Enforce floor of 1 tick AND ceiling of max_half_spread_pct of mid so
            # noisy books with EWMA spikes don't quote 20%+ wide and never get filled.
            half_spread = max(half_spread, 2 * tick)
            half_spread = min(half_spread, self.max_half_spread_pct * mid)

            target_buy = self._round_price(reservation - half_spread)
            target_sell = self._round_price(reservation + half_spread)

            # COMPETITIVE QUOTING + EDGE GATE.
            #
            # Always-front-of-queue quoting (target = bid+tick / ask-tick) gives
            # great fill rate but TERRIBLE adverse selection at zero edge.
            # Always-deep quoting gives great edge but near-zero fill rate.
            # The middle ground: quote at top-of-queue when the spread allows
            # at least min_edge there; otherwise quote DEEPER at the edge level
            # so we still leave a resting order with positive edge protection.
            has_room = (ask - bid) > (2 * tick)
            if has_room:
                top_buy = self._round_price(bid + tick)
                top_sell = self._round_price(ask - tick)
            else:
                top_buy = self._round_price(bid)
                top_sell = self._round_price(ask)
            min_edge = self.min_edge_bps * 1e-4 * mid
            buy_edge_at_top = mid - top_buy
            sell_edge_at_top = top_sell - mid
            edge_buy_ok_at_top = buy_edge_at_top >= min_edge
            edge_sell_ok_at_top = sell_edge_at_top >= min_edge

            # Decide whether/where to quote each side.
            quote_buy_edge = True
            quote_sell_edge = True
            if edge_buy_ok_at_top:
                # We can be at top-of-queue with edge. Use the more conservative
                # of model price and top-of-queue, but never below the bid.
                target_buy = max(min(target_buy, top_buy), self._round_price(bid))
                target_buy = min(target_buy, self._round_price(ask - tick))
                buy_at_top_quote = True
            elif self.min_edge_mode == "deeper":
                # Top-of-queue lacks edge. Quote DEEPER (passive, behind the
                # touch) at exactly the min-edge level. Won't fill often but
                # each fill is guaranteed edge >= min_edge_bps.
                deeper_buy = self._round_price(mid - min_edge)
                # Must still be a valid post-only price (not crossing).
                if deeper_buy < ask - 0.5 * tick:
                    target_buy = deeper_buy
                    buy_at_top_quote = False
                else:
                    quote_buy_edge = False
                    buy_at_top_quote = False
            else:  # mode == "skip"
                quote_buy_edge = False
                buy_at_top_quote = False

            if edge_sell_ok_at_top:
                target_sell = min(max(target_sell, top_sell), self._round_price(ask))
                target_sell = max(target_sell, self._round_price(bid + tick))
                sell_at_top_quote = True
            elif self.min_edge_mode == "deeper":
                deeper_sell = self._round_price(mid + min_edge)
                if deeper_sell > bid + 0.5 * tick:
                    target_sell = deeper_sell
                    sell_at_top_quote = False
                else:
                    quote_sell_edge = False
                    sell_at_top_quote = False
            else:
                quote_sell_edge = False
                sell_at_top_quote = False

            # Final non-crossing safeguard (only matters if both sides quoted).
            if quote_buy_edge and quote_sell_edge and target_sell <= target_buy:
                target_sell = self._round_price(target_buy + tick)
            # Re-export edge_*_ok in the names used downstream.
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

            # ----- 5. Inventory cap AND edge gate: decide whether to quote each side -----
            inv_cap_units = (self.inventory_cap_pct * per_book_wealth / mid) if mid > 0 else float("inf")
            quote_buy = (bs.inventory < inv_cap_units) and edge_buy_ok
            quote_sell = (bs.inventory > -inv_cap_units) and edge_sell_ok

            # ----- 6. Sizing: keep |inv| under cap so each fill is closeable next cycle -----
            qty = self._round_qty(self.base_qty)
            if qty <= 0:
                continue

            # ----- 7. Smart re-quote: only cancel/replace orders that have moved enough -----
            tol = self.requote_tolerance_pct * mid
            existing_buys = [o for o in account.orders if int(o.side) == int(OrderDirection.BUY)]
            existing_sells = [o for o in account.orders if int(o.side) == int(OrderDirection.SELL)]

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
                f"flat={diag['inv_flat_books']} sum_inv_base={diag['sum_inv_base']:+.2f} | "
                f"traded_vol={diag['sum_traded_volume']:.0f} cancels={diag['cancels']} "
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
