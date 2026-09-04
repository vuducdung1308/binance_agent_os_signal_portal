"""Plain-Python technical indicators — no numpy/pandas/TA-Lib dependency."""

from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    """Returns EMA values aligned to values[period-1:] (seeded with an SMA)."""
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for price in values[period:]:
        out.append((price - out[-1]) * multiplier + out[-1])
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI, aligned to values[period:]."""
    if len(values) < period + 1:
        return []
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))]

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))
    return out


def macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float]]:
    """Returns (macd_line, signal_line), both aligned to their own last elements
    (i.e. macd_line[-1] and signal_line[-1] are the latest values)."""
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    if not ema_fast or not ema_slow:
        return [], []
    offset = len(ema_fast) - len(ema_slow)  # fast EMA starts earlier (shorter period)
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
    """Wilder's ATR, aligned to closes[period+1:]."""
    if len(closes) < period + 2:
        return []
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    out = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        out.append((out[-1] * (period - 1) + tr) / period)
    return out


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
    """Wilder's ADX — trend-strength gate (not direction). Convention: ADX < 20 means
    no tradable trend, ADX > 25 means a trend strong enough to trade. Aligned to the
    end of the series like the other indicators here."""
    n = len(closes)
    if n < period * 2 + 1:
        return []

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    def wilder_smooth(values: list[float]) -> list[float]:
        out = [sum(values[:period])]
        for v in values[period:]:
            out.append(out[-1] - out[-1] / period + v)
        return out

    smoothed_plus = wilder_smooth(plus_dm)
    smoothed_minus = wilder_smooth(minus_dm)
    smoothed_tr = wilder_smooth(trs)

    plus_di = [100 * p / tr if tr else 0.0 for p, tr in zip(smoothed_plus, smoothed_tr)]
    minus_di = [100 * m / tr if tr else 0.0 for m, tr in zip(smoothed_minus, smoothed_tr)]
    dx = [100 * abs(p - m) / (p + m) if (p + m) else 0.0 for p, m in zip(plus_di, minus_di)]

    if len(dx) < period:
        return []
    out = [sum(dx[:period]) / period]
    for d in dx[period:]:
        out.append((out[-1] * (period - 1) + d) / period)
    return out
