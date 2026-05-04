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
  gamma=20.0              inventory aversion (higher = lean harder when long/short)
  vol_window=64           EWMA samples for sigma estimation
  inventory_cap_pct=0.10  hard cap on |inventory| as fraction of per-book wealth
  stop_loss_pct=0.005     close inventory when realized+unrealized loss > 0.5% of capital
  vol_pause_pct=0.70      pull quotes when traded_volume > 70% of cap
  expiry_seconds=4        GTT expiry of resting quotes (sim seconds)
  requote_tolerance_pct=0.0003  re-quote only if target shifts > 3 bps
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
        self.gamma: float = float(getattr(c, "gamma", 20.0))
        self.vol_window: int = int(getattr(c, "vol_window", 64))
        self.inventory_cap_pct: float = float(getattr(c, "inventory_cap_pct", 0.10))
        self.stop_loss_pct: float = float(getattr(c, "stop_loss_pct", 0.005))
        self.vol_pause_pct: float = float(getattr(c, "vol_pause_pct", 0.70))
        self.expiry_seconds: int = int(getattr(c, "expiry_seconds", 4))
        self.requote_tolerance_pct: float = float(getattr(c, "requote_tolerance_pct", 0.0003))
        self.capital_turnover_cap: float = float(getattr(c, "capital_turnover_cap", 1.0))

        # Per-book persistent state. Reset on simulation start.
        self.book_state: Dict[int, BookState] = {}

        bt.logging.info(
            "AdaptiveMarketMakerAgent initialized | "
            f"base_qty={self.base_qty} half_spread={self.base_half_spread} "
            f"gamma={self.gamma} stop_loss_pct={self.stop_loss_pct} "
            f"vol_pause_pct={self.vol_pause_pct} expiry_s={self.expiry_seconds}"
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

            # ----- 2. Stop-loss gate -----
            if self._maybe_stop_loss(response, book_id, bs, mid):
                # Cancel resting quotes on the same book; we are flattening this cycle.
                rest_ids = [o.id for o in account.orders] if account.orders else []
                if rest_ids:
                    response.cancel_orders(book_id=book_id, order_ids=rest_ids)
                continue

            # ----- 3. Volume cap gate -----
            if self._is_volume_gated(account, per_book_cap):
                # Already at >= vol_pause_pct of cap. Pull quotes; do not place new ones.
                rest_ids = [o.id for o in account.orders] if account.orders else []
                if rest_ids:
                    response.cancel_orders(book_id=book_id, order_ids=rest_ids)
                bt.logging.info(
                    f"BOOK {book_id} volume-gated: traded={getattr(account, 'traded_volume', 0)} "
                    f"cap={per_book_cap}; pulling quotes"
                )
                continue

            # ----- 4. Avellaneda-Stoikov style reservation + spread -----
            sigma2 = bs.sigma2
            inv_skew = self.gamma * bs.inventory * sigma2
            reservation = micro - inv_skew * mid  # sigma2 is dimensionless; scale by mid
            half_spread = self.base_half_spread * mid + self.gamma * sigma2 * mid
            # Enforce minimum half-spread of 1 tick to be marketable but profitable
            half_spread = max(half_spread, 2 * tick)

            target_buy = self._round_price(reservation - half_spread)
            target_sell = self._round_price(reservation + half_spread)

            # Make sure quotes never cross the book and are post-only-able.
            target_buy = min(target_buy, bid)        # post-only buy: must be <= best_bid
            target_sell = max(target_sell, ask)      # post-only sell: must be >= best_ask
            # And keep them non-crossing each other.
            if target_sell <= target_buy:
                target_sell = self._round_price(target_buy + tick)

            # ----- 5. Inventory cap: skew quoting to flatten -----
            inv_cap_units = (self.inventory_cap_pct * per_book_wealth / mid) if mid > 0 else float("inf")
            quote_buy = bs.inventory < inv_cap_units
            quote_sell = bs.inventory > -inv_cap_units

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

            # ----- 8. Place new buy quote -----
            if quote_buy and not keep_buy:
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

            # ----- 9. Place new sell quote -----
            if quote_sell and not keep_sell:
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

            bs.last_buy_price = target_buy
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
        return response


if __name__ == "__main__":
    """
    Standalone test against the local proxy. From inside `agents/`:
      python AdaptiveMarketMakerAgent.py --port 8888 --agent_id 0 \
          --params base_qty=0.5 base_half_spread=0.0008 gamma=20.0 \
                   stop_loss_pct=0.005 vol_pause_pct=0.70 expiry_seconds=4
    """
    launch(AdaptiveMarketMakerAgent)
