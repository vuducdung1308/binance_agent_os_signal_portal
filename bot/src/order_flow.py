"""Order-flow snapshot from Binance's public spot market data — taker buy/sell volume
split and order-book bid/ask imbalance. No API key needed (public endpoints).

Deliberately NOT wired into src/signal_engine.py's compute_entry_signal: Binance's
public API has no historical order-book endpoint, so a gate built on this could never
be backtested (see backtest.py). Every other condition in signal_engine.py is
walk-forward tested; adding one that can't be is how a strategy quietly stops being
evidence-based. This module is informational-only — logged alongside a fired signal
for the human reader's context, never used to allow or block a signal.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

SPOT_BASE_URL = "https://api.binance.com/api/v3"


@dataclass
class OrderFlowSnapshot:
    symbol: str
    taker_buy_pct: float
    taker_sell_pct: float
    bid_ask_ratio: float  # >1 = more resting buy liquidity than sell in the top book

    def as_line(self) -> str:
        return (
            f"Order flow ({self.symbol}, 1h, chỉ để tham khảo — không phải điều kiện tín hiệu): "
            f"taker buy {self.taker_buy_pct:.1f}% / sell {self.taker_sell_pct:.1f}%, "
            f"order book bid/ask ratio {self.bid_ask_ratio:.2f}"
        )


def get_order_flow_snapshot(symbol: str) -> OrderFlowSnapshot | None:
    try:
        raw = requests.get(
            f"{SPOT_BASE_URL}/klines", params={"symbol": symbol, "interval": "5m", "limit": 12}, timeout=10
        ).json()
        total_vol = sum(float(r[5]) for r in raw)
        taker_buy_vol = sum(float(r[9]) for r in raw)
        taker_buy_pct = (taker_buy_vol / total_vol * 100) if total_vol else 50.0

        depth = requests.get(f"{SPOT_BASE_URL}/depth", params={"symbol": symbol, "limit": 50}, timeout=10).json()
        bid_vol = sum(float(b[1]) for b in depth["bids"])
        ask_vol = sum(float(a[1]) for a in depth["asks"])
        bid_ask_ratio = (bid_vol / ask_vol) if ask_vol else 0.0

        return OrderFlowSnapshot(
            symbol=symbol,
            taker_buy_pct=taker_buy_pct,
            taker_sell_pct=100 - taker_buy_pct,
            bid_ask_ratio=bid_ask_ratio,
        )
    except Exception as exc:
        print(f"Không lấy được order flow cho {symbol}: {exc}")
        return None
