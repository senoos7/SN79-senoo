# SPDX-License-Identifier: MIT
from __future__ import annotations

import math
import time
from typing import Dict

import bittensor as bt

from taos.common.agents import launch
from taos.im.agents import FinanceSimulationAgent
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.events import TradeEvent, SimulationStartEvent
from taos.im.protocol.models import OrderDirection, STP, TimeInForce


# =========================================================
# STATE
# =========================================================
class BookState:
    __slots__ = (
        "inventory",
        "avg_cost",
        "realized",
        "last_mid",
        "sigma2",
        "ewma_s",
        "ewma_l",
        "last_buy",
        "last_sell",
        "stop_arm",
    )

    def __init__(self):
        self.inventory = 0.0
        self.avg_cost = 0.0
        self.realized = 0.0
        self.last_mid = 0.0
        self.sigma2 = 0.0
        self.ewma_s = 0.0
        self.ewma_l = 0.0
        self.last_buy = 0.0
        self.last_sell = 0.0
        self.stop_arm = True


# =========================================================
# AGENT
# =========================================================
class AdaptiveMarketMakerAgentV2(FinanceSimulationAgent):

    # --------------------------
    def initialize(self):
        c = self.config

        self.base_qty = float(getattr(c, "base_qty", 0.2))
        self.base_spread = float(getattr(c, "base_half_spread", 0.0008))
        self.gamma_base = float(getattr(c, "gamma", 6.0))

        self.inv_cap_pct = float(getattr(c, "inventory_cap_pct", 0.18))
        self.stop_loss_pct = float(getattr(c, "stop_loss_pct", 0.006))

        self.vol_window = int(getattr(c, "vol_window", 64))
        self.requote_tol = float(getattr(c, "requote_tolerance_pct", 0.002))

        self.max_spread = float(getattr(c, "max_half_spread_pct", 0.004))
        self.min_edge = float(getattr(c, "min_edge_bps", 0.5)) * 1e-4

        self.trend_alpha_s = 0.25
        self.trend_alpha_l = 0.05
        self.trend_th = 0.0008

        self.books: Dict[int, BookState] = {}
        self.cycle = 0

        bt.logging.info("V2 Elite Market Maker initialized")

    # --------------------------
    def onStart(self, event: SimulationStartEvent):
        self.books.clear()

    # --------------------------
    def onTrade(self, event: TradeEvent, validator: str = None):
        bs = self.books.setdefault(event.bookId, BookState())

        is_taker = event.takerAgentId == self.uid
        is_maker = event.makerAgentId == self.uid
        if not (is_taker or is_maker):
            return

        side = OrderDirection(event.side)
        our_side = side if is_taker else (
            OrderDirection.SELL if side == OrderDirection.BUY else OrderDirection.BUY
        )

        qty = float(event.quantity)
        px = float(event.price)
        signed = qty if our_side == OrderDirection.BUY else -qty

        prev = bs.inventory
        new = prev + signed

        if prev != 0 and (new == 0 or prev * new < 0):
            closed = min(abs(prev), qty)
            pnl = (px - bs.avg_cost) * (1 if prev > 0 else -1) * closed
            bs.realized += pnl

        if prev * signed > 0 or prev == 0:
            tot = abs(prev) + qty
            bs.avg_cost = (abs(prev) * bs.avg_cost + qty * px) / max(tot, 1e-9)
        elif new == 0:
            bs.avg_cost = 0.0
        elif prev * new < 0:
            bs.avg_cost = px

        bs.inventory = new

    # --------------------------
    def _vol(self, bs, mid):
        if bs.last_mid > 0:
            r = math.log(mid / bs.last_mid)
            a = 2 / (self.vol_window + 1)
            bs.sigma2 = (1 - a) * bs.sigma2 + a * r * r
        bs.last_mid = mid

        if bs.ewma_s == 0:
            bs.ewma_s = mid
            bs.ewma_l = mid
        else:
            bs.ewma_s = (1 - self.trend_alpha_s) * bs.ewma_s + self.trend_alpha_s * mid
            bs.ewma_l = (1 - self.trend_alpha_l) * bs.ewma_l + self.trend_alpha_l * mid

    def _rq(self, q: float) -> float:
        return round(q, self.simulation_config.volumeDecimals)

    # --------------------------
    def respond(self, state: MarketSimulationStateUpdate):
        t0 = time.time()
        resp = FinanceAgentResponse(agent_id=self.uid)

        self.cycle += 1

        for book_id, book in state.books.items():
            if not book.bids or not book.asks:
                continue

            bs = self.books.setdefault(book_id, BookState())
            acc = self.accounts.get(book_id)
            if not acc:
                continue

            bid = float(book.bids[0].price)
            ask = float(book.asks[0].price)
            bq = float(book.bids[0].quantity)
            aq = float(book.asks[0].quantity)

            mid = (bid + ask) * 0.5
            micro = (bq * ask + aq * bid) / max(bq + aq, 1e-9)

            self._vol(bs, mid)

            sigma = math.sqrt(bs.sigma2 + 1e-12)

            inv = bs.inventory

            # ---------------- dynamic risk ----------------
            inv_pressure = inv * sigma
            gamma = self.gamma_base * (1 + abs(inv_pressure))

            skew = gamma * bs.sigma2 * mid * inv

            reservation = micro - skew

            spread = self.base_spread * mid + gamma * sigma * mid
            spread = min(max(spread, 2e-5), self.max_spread * mid)

            buy_px = reservation - spread
            sell_px = reservation + spread

            # ---------------- regime detection ----------------
            trend = (bs.ewma_s - bs.ewma_l) / (mid + 1e-9)
            up = trend > self.trend_th
            down = trend < -self.trend_th

            inv_cap = self.inv_cap_pct * 400 / mid if mid > 0 else 0.0

            long = inv > inv_cap
            short = inv < -inv_cap

            # ---------------- stop loss ----------------
            unreal = (mid - bs.avg_cost) * inv if inv != 0 else 0
            if unreal < -self.stop_loss_pct * 400:
                qty = self._rq(abs(inv))
                if qty > 0:
                    resp.market_order(
                        book_id=book_id,
                        direction=OrderDirection.SELL if inv > 0 else OrderDirection.BUY,
                        quantity=qty,
                    )
                continue

            # ---------------- inventory skew logic ----------------
            if long:
                buy_ok = False
                sell_ok = True
            elif short:
                buy_ok = True
                sell_ok = False
            else:
                buy_ok = True
                sell_ok = True

            if up and inv < 0:
                sell_ok = False
            if down and inv > 0:
                buy_ok = False

            # ---------------- price normalization ----------------
            tick = 10 ** (-self.simulation_config.priceDecimals)

            buy_px = min(buy_px, ask - tick)
            sell_px = max(sell_px, bid + tick)

            # ---------------- order reuse ----------------
            existing = acc.orders or []
            keep_buy = any(
                abs(float(o.price) - buy_px) < self.requote_tol * mid
                and int(o.side) == int(OrderDirection.BUY)
                for o in existing
            )
            keep_sell = any(
                abs(float(o.price) - sell_px) < self.requote_tol * mid
                and int(o.side) == int(OrderDirection.SELL)
                for o in existing
            )

            if not keep_buy:
                resp.cancel_orders(book_id=book_id, order_ids=[
                    o.id for o in existing if int(o.side) == int(OrderDirection.BUY)
                ])
            if not keep_sell:
                resp.cancel_orders(book_id=book_id, order_ids=[
                    o.id for o in existing if int(o.side) == int(OrderDirection.SELL)
                ])

            # ---------------- execution (taker-first hybrid MM) ----------------
            qty = self.base_qty

            if buy_ok and not keep_buy and acc.quote_balance.free > qty * ask:
                resp.limit_order(
                    book_id=book_id,
                    direction=OrderDirection.BUY,
                    quantity=qty,
                    price=ask,
                    timeInForce=TimeInForce.GTT,
                    expiryPeriod=30_000_000_000,
                    stp=STP.CANCEL_BOTH,
                )

            if sell_ok and not keep_sell and acc.base_balance.free > qty:
                resp.limit_order(
                    book_id=book_id,
                    direction=OrderDirection.SELL,
                    quantity=qty,
                    price=bid,
                    timeInForce=TimeInForce.GTT,
                    expiryPeriod=30_000_000_000,
                    stp=STP.CANCEL_BOTH,
                )

        if time.time() - t0 > 0.25:
            bt.logging.warning("V2 slow cycle")

        return resp


# =========================================================
if __name__ == "__main__":
    launch(AdaptiveMarketMakerAgentV2)
