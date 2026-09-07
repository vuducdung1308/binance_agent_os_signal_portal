"""Chart analytics for the coin modal — computed from raw Binance klines (the
still-forming candle included, since this is display, not signal input).

Everything here is heuristic price-structure stuff people draw by hand:

  - support / resistance zones   (clustered swing pivots)
  - auto trendlines              (last two swing lows / highs, extended, broken flag)
  - RSI divergence               (price HH + RSI LH → bearish; price LL + RSI HL → bullish)
  - liquidity                    (recent swing highs/lows + equal-high / equal-low pools)
  - volume + volume profile      (POC / value area)

None of it feeds the signal engine. It's a drawing aid.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import bot_bridge as bb
from .market import klines_raw


# --------------------------------------------------------------------------- pivots
def _pivots(values: List[float], left: int, right: int, kind: str) -> List[int]:
    out: List[int] = []
    n = len(values)
    for i in range(left, n - right):
        v = values[i]
        window = values[i - left : i + right + 1]
        if kind == "high" and v == max(window) and window.count(v) == 1:
            out.append(i)
        elif kind == "low" and v == min(window) and window.count(v) == 1:
            out.append(i)
    return out


def _atr(highs: List[float], lows: List[float], closes: List[float]) -> float:
    try:
        a = bb.atr(highs, lows, closes, 14)
        return a[-1] if a else 0.0
    except Exception:
        return 0.0


def _rsi_full(closes: List[float], period: int = 14) -> List[Optional[float]]:
    r = bb.rsi(closes, period)
    return [None] * (len(closes) - len(r)) + list(r)


# --------------------------------------------------------------------- S/R zones
def _sr_zones(candles: List[dict], atr: float, max_zones: int = 5) -> List[dict]:
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    span = max(highs) - min(lows)
    if span <= 0:
        return []
    tol = max(atr * 0.6, span * 0.004)      # merge distance
    max_width = max(tol * 2.5, span * 0.012)  # hard cap on a zone's width

    marks = sorted(
        [(highs[i], candles[i]["t"]) for i in _pivots(highs, 3, 3, "high")]
        + [(lows[i], candles[i]["t"]) for i in _pivots(lows, 3, 3, "low")],
        key=lambda m: m[0],
    )
    if not marks:
        return []

    clusters: List[dict] = []
    for price, t in marks:
        if clusters:
            cl = clusters[-1]
            if price - cl["lo"] <= max_width and price - cl["hi"] <= tol:
                cl["prices"].append(price)
                cl["hi"] = price
                cl["last_t"] = max(cl["last_t"], t)
                continue
        clusters.append({"prices": [price], "lo": price, "hi": price, "last_t": t})

    cur = candles[-1]["c"]
    zones = []
    for cl in clusters:
        ps = cl["prices"]
        mid = sum(ps) / len(ps)
        zones.append({
            "low": round(min(ps), 8), "high": round(max(ps), 8), "mid": round(mid, 8),
            "touches": len(ps),
            "kind": "resistance" if mid >= cur else "support",
            "last_t": cl["last_t"],
        })
    strong = [z for z in zones if z["touches"] >= 2]
    strong.sort(key=lambda z: (z["touches"], z["last_t"]), reverse=True)
    if len(strong) < 3:  # top up with the most recent single touches
        singles = sorted((z for z in zones if z["touches"] < 2),
                         key=lambda z: z["last_t"], reverse=True)
        strong += singles[: 3 - len(strong)]
    return strong[:max_zones]


# --------------------------------------------------------------------- trendlines
def _trendline(candles: List[dict], pivot_idx: List[int], series: List[float], kind: str) -> Optional[dict]:
    if len(pivot_idx) < 2:
        return None
    i1, i2 = pivot_idx[-2], pivot_idx[-1]
    if i2 <= i1:
        return None
    p1, p2 = series[i1], series[i2]
    slope = (p2 - p1) / (i2 - i1)
    last = len(candles) - 1
    end_price = p2 + slope * (last - i2)

    broken_t = None
    for j in range(i2 + 1, len(candles)):
        line_p = p2 + slope * (j - i2)
        c = candles[j]["c"]
        if kind == "up" and c < line_p * 0.999:
            broken_t = candles[j]["t"]
            break
        if kind == "down" and c > line_p * 1.001:
            broken_t = candles[j]["t"]
            break

    return {
        "kind": kind,
        "points": [
            {"t": candles[i1]["t"], "price": round(p1, 8)},
            {"t": candles[last]["t"], "price": round(end_price, 8)},
        ],
        "broken": broken_t is not None,
        "broken_t": broken_t,
    }


# --------------------------------------------------------------------- divergence
def _divergences(candles, highs, lows, rsi_full, lookback: int = 140) -> List[dict]:
    n = len(candles)
    start = max(0, n - lookback)
    piv_h = [i for i in _pivots(highs, 3, 3, "high") if i >= start and rsi_full[i] is not None]
    piv_l = [i for i in _pivots(lows, 3, 3, "low") if i >= start and rsi_full[i] is not None]
    out: List[dict] = []

    for a, b in zip(piv_h, piv_h[1:]):
        if highs[b] > highs[a] and rsi_full[b] < rsi_full[a] - 1:
            out.append({
                "kind": "bearish",
                "price": [{"t": candles[a]["t"], "v": highs[a]}, {"t": candles[b]["t"], "v": highs[b]}],
                "rsi": [{"t": candles[a]["t"], "v": round(rsi_full[a], 1)},
                        {"t": candles[b]["t"], "v": round(rsi_full[b], 1)}],
            })
    for a, b in zip(piv_l, piv_l[1:]):
        if lows[b] < lows[a] and rsi_full[b] > rsi_full[a] + 1:
            out.append({
                "kind": "bullish",
                "price": [{"t": candles[a]["t"], "v": lows[a]}, {"t": candles[b]["t"], "v": lows[b]}],
                "rsi": [{"t": candles[a]["t"], "v": round(rsi_full[a], 1)},
                        {"t": candles[b]["t"], "v": round(rsi_full[b], 1)}],
            })
    out.sort(key=lambda d: d["price"][1]["t"])
    return out[-4:]


# --------------------------------------------------------------------- liquidity
def _liquidity(candles, highs, lows, atr: float, lookback: int = 90) -> dict:
    n = len(candles)
    start = max(0, n - lookback)
    hi_idx = [i for i in _pivots(highs, 2, 2, "high") if i >= start]
    lo_idx = [i for i in _pivots(lows, 2, 2, "low") if i >= start]
    swings = (
        [{"t": candles[i]["t"], "price": highs[i], "kind": "high"} for i in hi_idx]
        + [{"t": candles[i]["t"], "price": lows[i], "kind": "low"} for i in lo_idx]
    )
    tol = max(atr * 0.35, (max(highs) - min(lows)) * 0.002)

    def _pools(idx, series, kind):
        pools = []
        used = set()
        for a in range(len(idx)):
            if a in used:
                continue
            group = [idx[a]]
            for b in range(a + 1, len(idx)):
                if abs(series[idx[b]] - series[idx[a]]) <= tol:
                    group.append(idx[b]); used.add(b)
            if len(group) >= 2:
                pools.append({
                    "price": sum(series[g] for g in group) / len(group),
                    "count": len(group), "kind": kind,
                    "last_t": max(candles[g]["t"] for g in group),
                })
        return pools

    pools = _pools(hi_idx, highs, "sell-side") + _pools(lo_idx, lows, "buy-side")
    pools.sort(key=lambda p: (p["count"], p["last_t"]), reverse=True)
    return {"swings": swings, "pools": pools[:6]}


# --------------------------------------------------------------- volume profile
def _volume_profile(candles: List[dict], bins: int = 26) -> dict:
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    lo, hi = min(lows), max(highs)
    if hi <= lo:
        return {}
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for c in candles:
        b = int((c["c"] - lo) / step)
        vol[min(max(b, 0), bins - 1)] += c["v"]

    total = sum(vol) or 1.0
    poc_b = max(range(bins), key=lambda i: vol[i])
    order = sorted(range(bins), key=lambda i: vol[i], reverse=True)
    acc, va = 0.0, set()
    for i in order:
        acc += vol[i]
        va.add(i)
        if acc >= total * 0.7:
            break
    return {
        "poc": lo + (poc_b + 0.5) * step,
        "value_area_low": lo + min(va) * step,
        "value_area_high": lo + (max(va) + 1) * step,
        "max_bin_volume": max(vol),
        "bins": [{"price": lo + (i + 0.5) * step, "volume": round(vol[i], 4)} for i in range(bins)],
    }


# --------------------------------------------------------------------- assemble
def analyze_chart(symbol: str, interval: str, limit: int) -> Dict[str, Any]:
    raw = klines_raw(symbol.upper(), interval, limit)
    candles = [
        {"t": r[0] // 1000, "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
        for r in raw
    ]
    if len(candles) < 30:
        return {"symbol": symbol.upper(), "interval": interval, "candles": candles}

    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]
    atr = _atr(highs, lows, closes)
    rsi_full = _rsi_full(closes)

    ema_fast = bb.ema(closes, 50)
    ema_slow = bb.ema(closes, 200)

    def _line(series, period):
        pad = len(closes) - len(series)
        return [{"t": candles[pad + i]["t"], "value": round(v, 8)} for i, v in enumerate(series)]

    piv_lo = _pivots(lows, 3, 3, "low")
    piv_hi = _pivots(highs, 3, 3, "high")

    return {
        "symbol": symbol.upper(),
        "interval": interval,
        "candles": candles,
        "ema": {"fast": _line(ema_fast, 50), "slow": _line(ema_slow, 200)},
        "rsi": [
            {"t": candles[i]["t"], "value": round(v, 2)}
            for i, v in enumerate(rsi_full) if v is not None
        ],
        "sr_zones": _sr_zones(candles, atr),
        "trendlines": [
            t for t in (
                _trendline(candles, piv_lo, lows, "up"),
                _trendline(candles, piv_hi, highs, "down"),
            ) if t
        ],
        "divergences": _divergences(candles, highs, lows, rsi_full),
        "liquidity": _liquidity(candles, highs, lows, atr),
        "volume_profile": _volume_profile(candles),
        "atr": round(atr, 8),
    }
