"""Lightweight, high-frequency price check for OPEN positions only — just the live
ticker price against TP1/TP2/SL, no full candle history, no indicators, no AI call.

This is intentionally separate from the hourly signal scan (main_signals.py): scanning
for NEW entries must use closed candles only (see price_data.py's _drop_unclosed — using
a still-forming candle there causes repainting/flip-flopping signals). But watching an
ALREADY-OPEN position for a TP/SL price cross has no such look-ahead concern — it's
just "has the real price crossed this exact number" — so checking the live price
frequently here catches a fast intra-hour move that the hourly closed-candle scan would
otherwise miss for up to ~an hour.

Technical-reversal exits (RSI/MACD based) still need indicator history and stay on the
slower hourly cadence in main_signals.py.
"""

from __future__ import annotations

import requests

from .signal_engine import ExitEvent

TICKER_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"


def fetch_current_price(symbol: str) -> float:
    resp = requests.get(TICKER_PRICE_URL, params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    return float(resp.json()["price"])


def compute_price_only_exit(position: dict, current_price: float) -> ExitEvent | None:
    """Checks only SL/TP1/TP2 crossing against the live price — no RSI/MACD, so no
    "technical_reversal" reason can come from here."""
    direction = position["direction"]
    symbol = position["symbol"]

    if direction == "LONG":
        if current_price <= position["stop_loss"]:
            return ExitEvent(symbol, direction, "sl_hit", current_price, rsi_value=0.0)
        if not position.get("tp1_hit") and current_price >= position["take_profit_1"]:
            return ExitEvent(symbol, direction, "tp1_hit", current_price, rsi_value=0.0)
        if current_price >= position["take_profit_2"]:
            return ExitEvent(symbol, direction, "tp2_hit", current_price, rsi_value=0.0)
    else:  # SHORT
        if current_price >= position["stop_loss"]:
            return ExitEvent(symbol, direction, "sl_hit", current_price, rsi_value=0.0)
        if not position.get("tp1_hit") and current_price <= position["take_profit_1"]:
            return ExitEvent(symbol, direction, "tp1_hit", current_price, rsi_value=0.0)
        if current_price <= position["take_profit_2"]:
            return ExitEvent(symbol, direction, "tp2_hit", current_price, rsi_value=0.0)

    return None
