# SPDX-License-Identifier: MIT
"""
HybridMarketMakerAgent — OBI + Momentum + AS-skew signal → IOC taker orders

Strategy:
  1. Compute a composite signal per book:
       signal = obi_weight * OBI + mom_weight * momentum + as_skew_weight * AS_skew
     OBI        = (bid_qty - ask_qty) / (bid_qty + ask_qty)  at top `obi_depth` levels
     momentum   = clip((ewma_short - ewma_long) / ewma_long / mom_norm, -1, 1)
     AS_skew    = clip(-gamma * inventory * sigma2 * as_skew_scale, -1, 1)
  2. signal > +threshold  → IOC BUY at ask  (immediate taker fill)
     signal < -threshold  → IOC SELL at bid (immediate taker fill)
  3. URGENT flatten (inv_ratio ≥ urgent_threshold): ignore signal, force-close via IOC
  4. Stop-loss: market_order for 50% when unrealized_pnl < -stop_loss_pct * wealth

Why IOC instead of postOnly GTT:
  The live SN-79 subnet has no noise takers to fill resting maker orders.
  4000+ cycles of postOnly=True at bid/ask produced zero fills. IOC at ask/bid
  crosses immediately — matching the reference ImbalanceAgent pattern.

Tunable parameters (pass via --params key=value):
  base_qty            float   0.2      Base order size in BASE currency
  inventory_cap_pct   float   0.15     Max |inventory| as fraction of per-book wealth / mid
  urgent_threshold    float   0.80     inv_ratio above which urgent flatten triggers
  gamma               float   6.0      AS model risk-aversion (higher = tighter inventory)
  vol_window          int     50       EWMA window for σ² (in cycles)
  obi_depth           int     5        Order book levels for OBI calculation
  obi_weight          float   0.40     OBI component weight in composite signal
  mom_weight          float   0.40     Momentum component weight
  as_skew_weight      float   0.20     AS inventory-skew component weight
  mom_norm            float   0.001    Normaliser for momentum (0.001 = 10 bps clips to ±1)
  as_skew_scale       float   100.0    Scales γ·inv·σ² into [-1,+1] range
  ewma_short_alpha    float   0.30     α for short EWMA of microprice
  ewma_long_alpha     float   0.05     α for long EWMA of microprice
  signal_threshold    float   0.10     Min |signal| required to place an IOC order
  stop_loss_pct       float   0.01     Stop-loss at this fraction of per-book wealth
  diag_every          int     20       Log DIAG every N cycles
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
from taos.im.protocol.models import OrderDirection, TimeInForce, STP


# ── per-book state ──────────────────────────────────────────────────────────
class BookState:
    __slots__ = (
        "inventory", "avg_cost", "cum_realized_pnl",
        "last_mid", "sigma2",
        "ewma_short", "ewma_long",
    )

    def __init__(self):
        self.inventory = 0.0
        self.avg_cost = 0.0
        self.cum_realized_pnl = 0.0
        self.last_mid = 0.0
        self.sigma2 = 1e-8
        self.ewma_short = 0.0
        self.ewma_long = 0.0


# ── agent ───────────────────────────────────────────────────────────────────
class HybridMarketMakerAgent(FinanceSimulationAgent):

    def initialize(self):
        c = self.config

        # Position sizing
        self.base_qty            = float(getattr(c, "base_qty",            0.2))
        self.inventory_cap_pct   = float(getattr(c, "inventory_cap_pct",   0.15))
        self.urgent_threshold    = float(getattr(c, "urgent_threshold",    0.80))

        # AS model
        self.gamma               = float(getattr(c, "gamma",               6.0))
        self.vol_window          = int(getattr(c,   "vol_window",          50))

        # Signal
        self.obi_depth           = int(getattr(c,   "obi_depth",           5))
        self.obi_weight          = float(getattr(c, "obi_weight",          0.40))
        self.mom_weight          = float(getattr(c, "mom_weight",          0.40))
        self.as_skew_weight      = float(getattr(c, "as_skew_weight",      0.20))
        self.mom_norm            = float(getattr(c, "mom_norm",            0.001))
        self.as_skew_scale       = float(getattr(c, "as_skew_scale",       100.0))
        self.ewma_short_alpha    = float(getattr(c, "ewma_short_alpha",    0.30))
        self.ewma_long_alpha     = float(getattr(c, "ewma_long_alpha",     0.05))
        self.signal_threshold    = float(getattr(c, "signal_threshold",    0.10))

        # Risk
        self.stop_loss_pct       = float(getattr(c, "stop_loss_pct",       0.01))

        # Diagnostics
        self.diag_every          = int(getattr(c,   "diag_every",          20))
        self._cycle              = 0

        self.book_state: Dict[int, BookState] = {}
        bt.logging.info("HybridMarketMakerAgent initialized")

    def onStart(self, event: SimulationStartEvent):
        self.book_state.clear()
        self._cycle = 0

    def onTrade(self, event: TradeEvent, validator=None):
        bs = self.book_state.setdefault(event.bookId, BookState())
        if event.takerAgentId != self.uid and event.makerAgentId != self.uid:
            return

        qty   = float(event.quantity)
        price = float(event.price)
        side  = OrderDirection(event.side)

        our_side = side if event.takerAgentId == self.uid else (
            OrderDirection.SELL if side == OrderDirection.BUY else OrderDirection.BUY
        )
        signed_qty = qty if our_side == OrderDirection.BUY else -qty

        prev_inv     = bs.inventory
        bs.inventory += signed_qty

        if prev_inv == 0 or (prev_inv * signed_qty > 0):
            # Adding to or opening a position
            total        = abs(prev_inv) + qty
            bs.avg_cost  = (abs(prev_inv) * bs.avg_cost + qty * price) / total
        elif bs.inventory == 0:
            # Full close
            bs.cum_realized_pnl += (price - bs.avg_cost) * prev_inv
            bs.avg_cost = 0.0
        else:
            # Partial close or flip
            close_qty = min(abs(prev_inv), qty)
            bs.cum_realized_pnl += (price - bs.avg_cost) * close_qty * (1 if prev_inv > 0 else -1)
            if prev_inv * bs.inventory < 0:
                bs.avg_cost = price  # flipped sides, reset cost basis

    # ── helpers ──────────────────────────────────────────────────────────────

    def _wealth_per_book(self) -> float:
        cfg = self.simulation_config
        w = float(getattr(cfg, "miner_wealth", 0.0))
        n = max(1, int(getattr(cfg, "book_count", 1)))
        return w / n if w > 0 else 0.0

    def _update_vol(self, bs: BookState, mid: float):
        if bs.last_mid > 0:
            r        = math.log(mid / bs.last_mid)
            alpha    = 2.0 / (self.vol_window + 1)
            bs.sigma2 = (1 - alpha) * bs.sigma2 + alpha * r * r
        bs.last_mid = mid

    def _compute_signal(self, bs: BookState, book, bid: float, ask: float) -> float:
        depth   = self.obi_depth
        bid_qty = sum(float(b.quantity) for b in book.bids[:depth])
        ask_qty = sum(float(a.quantity) for a in book.asks[:depth])
        total_q = bid_qty + ask_qty

        # Order Book Imbalance ∈ [-1, +1]
        obi = (bid_qty - ask_qty) / max(total_q, 1e-9)

        # Microprice momentum
        micro = (bid * ask_qty + ask * bid_qty) / max(total_q, 1e-9)
        if bs.ewma_short == 0.0:
            bs.ewma_short = micro
            bs.ewma_long  = micro
        else:
            bs.ewma_short = (1 - self.ewma_short_alpha) * bs.ewma_short + self.ewma_short_alpha * micro
            bs.ewma_long  = (1 - self.ewma_long_alpha)  * bs.ewma_long  + self.ewma_long_alpha  * micro
        mom_raw  = (bs.ewma_short - bs.ewma_long) / max(bs.ewma_long, 1e-9)
        momentum = max(-1.0, min(1.0, mom_raw / self.mom_norm))

        # AS inventory skew ∈ [-1, +1]
        # Negative inventory (short) → as_bias > 0 → want to buy
        # Positive inventory (long)  → as_bias < 0 → want to sell
        as_bias = max(-1.0, min(1.0, -self.gamma * bs.inventory * bs.sigma2 * self.as_skew_scale))

        return (
            self.obi_weight     * obi
            + self.mom_weight   * momentum
            + self.as_skew_weight * as_bias
        )

    def _round_price(self, p: float) -> float:
        return round(p, self.simulation_config.priceDecimals)

    def _round_qty(self, q: float) -> float:
        return round(q, self.simulation_config.volumeDecimals)

    # ── main loop ─────────────────────────────────────────────────────────────

    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        t0 = time.time()
        self._cycle += 1
        response = FinanceAgentResponse(agent_id=self.uid)
        wpb = self._wealth_per_book()

        diag = dict(
            books=0,
            buy_ioc=0, sell_ioc=0, market_sl=0,
            urgent_buy=0, urgent_sell=0, skip_bal=0,
            signal_buy=0, signal_sell=0, signal_flat=0,
            long=0, short=0, flat=0,
            sum_inv=0.0, realized_pnl=0.0,
        )

        for book_id, book in state.books.items():
            if not book.bids or not book.asks:
                continue

            bs      = self.book_state.setdefault(book_id, BookState())
            account = self.accounts.get(book_id)
            if account is None:
                continue

            diag["books"] += 1
            bid = float(book.bids[0].price)
            ask = float(book.asks[0].price)
            mid = 0.5 * (bid + ask)

            self._update_vol(bs, mid)

            inv_cap   = self.inventory_cap_pct * wpb / mid if mid > 0 else 1.0
            inv_ratio = abs(bs.inventory) / max(inv_cap, 1e-9)
            URGENT    = inv_ratio >= self.urgent_threshold

            qty = self._round_qty(self.base_qty)
            if qty <= 0:
                continue

            # ── composite signal ─────────────────────────────────────────────
            signal    = self._compute_signal(bs, book, bid, ask)
            want_buy  = signal >  self.signal_threshold
            want_sell = signal < -self.signal_threshold

            if want_buy:
                diag["signal_buy"]  += 1
            elif want_sell:
                diag["signal_sell"] += 1
            else:
                diag["signal_flat"] += 1

            # ── BUY ──────────────────────────────────────────────────────────
            if bs.inventory < inv_cap:
                if URGENT and bs.inventory < 0:
                    # Force-close short: IOC at ask regardless of signal
                    close_qty = self._round_qty(min(abs(bs.inventory), qty))
                    if close_qty > 0 and account.quote_balance.free >= close_qty * ask:
                        response.limit_order(
                            book_id=book_id,
                            direction=OrderDirection.BUY,
                            quantity=close_qty,
                            price=self._round_price(ask),
                            timeInForce=TimeInForce.IOC,
                            stp=STP.CANCEL_BOTH,
                        )
                        diag["urgent_buy"] += 1
                        diag["buy_ioc"]    += 1
                elif want_buy and not URGENT:
                    cost = qty * ask
                    if account.quote_balance.free >= cost:
                        response.limit_order(
                            book_id=book_id,
                            direction=OrderDirection.BUY,
                            quantity=qty,
                            price=self._round_price(ask),
                            timeInForce=TimeInForce.IOC,
                            stp=STP.CANCEL_BOTH,
                        )
                        diag["buy_ioc"] += 1
                    else:
                        diag["skip_bal"] += 1

            # ── SELL ─────────────────────────────────────────────────────────
            if bs.inventory > -inv_cap:
                if URGENT and bs.inventory > 0:
                    # Force-close long: IOC at bid regardless of signal
                    close_qty = self._round_qty(min(abs(bs.inventory), qty))
                    if close_qty > 0 and account.base_balance.free >= close_qty:
                        response.limit_order(
                            book_id=book_id,
                            direction=OrderDirection.SELL,
                            quantity=close_qty,
                            price=self._round_price(bid),
                            timeInForce=TimeInForce.IOC,
                            stp=STP.CANCEL_BOTH,
                        )
                        diag["urgent_sell"] += 1
                        diag["sell_ioc"]    += 1
                elif want_sell and not URGENT:
                    if account.base_balance.free >= qty:
                        response.limit_order(
                            book_id=book_id,
                            direction=OrderDirection.SELL,
                            quantity=qty,
                            price=self._round_price(bid),
                            timeInForce=TimeInForce.IOC,
                            stp=STP.CANCEL_BOTH,
                        )
                        diag["sell_ioc"] += 1
                    else:
                        diag["skip_bal"] += 1

            # ── stop-loss (market order, always fills) ────────────────────────
            if bs.inventory != 0:
                unrealized = (mid - bs.avg_cost) * bs.inventory
                if unrealized < -self.stop_loss_pct * wpb:
                    sl_qty = self._round_qty(abs(bs.inventory) * 0.5)
                    if sl_qty > 0:
                        direction = (
                            OrderDirection.SELL if bs.inventory > 0 else OrderDirection.BUY
                        )
                        response.market_order(
                            book_id=book_id,
                            direction=direction,
                            quantity=sl_qty,
                        )
                        diag["market_sl"] += 1

            # ── inventory tracking ───────────────────────────────────────────
            diag["sum_inv"]       += bs.inventory
            diag["realized_pnl"]  += bs.cum_realized_pnl
            if bs.inventory > 1e-9:
                diag["long"]  += 1
            elif bs.inventory < -1e-9:
                diag["short"] += 1
            else:
                diag["flat"]  += 1

        elapsed = time.time() - t0
        if elapsed > 0.2:
            bt.logging.warning(f"HybridMarketMaker slow: {elapsed:.3f}s")

        if self._cycle % self.diag_every == 0:
            bt.logging.info(
                f"DIAG c={self._cycle} wealth={int(wpb * max(diag['books'], 1))} "
                f"books={diag['books']} "
                f"| signal: buy={diag['signal_buy']} sell={diag['signal_sell']} flat={diag['signal_flat']} "
                f"| orders: buy_ioc={diag['buy_ioc']} sell_ioc={diag['sell_ioc']} sl={diag['market_sl']} "
                f"urgent_buy={diag['urgent_buy']} urgent_sell={diag['urgent_sell']} skip_bal={diag['skip_bal']} "
                f"| inv: long={diag['long']} short={diag['short']} flat={diag['flat']} "
                f"sum={diag['sum_inv']:+.2f} "
                f"| realized={diag['realized_pnl']:+.2f} "
                f"| instr={len(response.instructions)} resp_ms={int(elapsed * 1000)}"
            )

        return response


if __name__ == "__main__":
    launch(HybridMarketMakerAgent)
