"""Fetch OHLCV candles from Binance's public market-data API (no key needed)."""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

KLINES_URL = "https://api.binance.com/api/v3/klines"
MAX_LIMIT_PER_REQUEST = 1000


@dataclass
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _parse_klines(raw: list) -> list[Candle]:
    return [
        Candle(
            open_time=row[0],
            close_time=row[6],
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in raw
    ]


def _drop_unclosed(candles: list[Candle]) -> list[Candle]:
    """Binance's klines endpoint includes the currently-forming candle as the last
    row — using it means every indicator (RSI/EMA/MACD/ATR) keeps changing value as
    that candle fills in, causing signals to flicker in and out."""
    now_ms = int(time.time() * 1000)
    while candles and candles[-1].close_time > now_ms:
        candles.pop()
    return candles


def fetch_klines(symbol: str, interval: str = "1h", limit: int = 300) -> list[Candle]:
    """symbol like "BTCUSDT". Returns oldest-first, most recent CLOSED candle last."""
    resp = requests.get(
        KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": limit + 1},
        timeout=15,
    )
    resp.raise_for_status()
    candles = _drop_unclosed(_parse_klines(resp.json()))
    return candles[-limit:]


def fetch_klines_history(symbol: str, interval: str = "1h", total_candles: int = 2000) -> list[Candle]:
    """Like fetch_klines but paginates backward past Binance's 1000-candle-per-request
    cap (for backtesting over a longer span than one request covers)."""
    collected: list[Candle] = []
    end_time: int | None = None

    while len(collected) < total_candles:
        params = {"symbol": symbol, "interval": interval, "limit": MAX_LIMIT_PER_REQUEST}
        if end_time is not None:
            params["endTime"] = end_time
        resp = requests.get(KLINES_URL, params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        if not raw:
            break
        batch = _parse_klines(raw)
        collected = batch + collected
        end_time = batch[0].open_time - 1
        if len(raw) < MAX_LIMIT_PER_REQUEST:
            break  # exhausted available history

    collected = _drop_unclosed(collected)
    return collected[-total_candles:]
