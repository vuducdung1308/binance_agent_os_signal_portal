"""Small direct Binance public-API helpers the portal needs but the bot doesn't have
in this exact shape:

  - ticker_24hr(symbols): one batched request -> live price, 24h volume, 24h % change
  - klines_raw(...): candles INCLUDING the still-forming one, for the price chart only
    (the bot's fetch_klines deliberately drops it — correct for signals, wrong for a
    live chart that should show the current bar)
  - symbol_exists(symbol): validate a coin before adding it to the watchlist

All public endpoints, no API key.
"""

from __future__ import annotations

import json
from typing import Dict, List

import requests

BASE = "https://api.binance.com/api/v3"
_TIMEOUT = 12


def ticker_24hr(symbols: List[str]) -> Dict[str, dict]:
    """Returns {symbol: {price, change_pct, high, low, quote_volume, volume}}."""
    if not symbols:
        return {}
    resp = requests.get(
        f"{BASE}/ticker/24hr",
        params={"symbols": json.dumps(symbols, separators=(",", ":"))},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    out: Dict[str, dict] = {}
    for row in resp.json():
        out[row["symbol"]] = {
            "price": float(row["lastPrice"]),
            "change_pct": float(row["priceChangePercent"]),
            "high": float(row["highPrice"]),
            "low": float(row["lowPrice"]),
            "quote_volume": float(row["quoteVolume"]),
            "volume": float(row["volume"]),
        }
    return out


def klines_raw(symbol: str, interval: str = "1h", limit: int = 300) -> List[list]:
    """[[open_time, open, high, low, close, volume, close_time], ...] oldest-first,
    INCLUDING the currently-forming candle."""
    resp = requests.get(
        f"{BASE}/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        [r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), r[6]]
        for r in resp.json()
    ]


def symbol_exists(symbol: str) -> bool:
    try:
        resp = requests.get(f"{BASE}/ticker/price", params={"symbol": symbol}, timeout=_TIMEOUT)
        return resp.status_code == 200 and "price" in resp.json()
    except Exception:
        return False
