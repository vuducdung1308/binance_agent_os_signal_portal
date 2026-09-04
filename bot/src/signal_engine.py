"""Rule-based (non-AI) trade signal generation from price data.

All numbers here are computed deterministically; the AI layer only writes narrative
text around them, never invents entry/SL/TP/exit prices itself.

Entry setups (checked in this priority order — first match wins, one signal per coin
per check):

1. LONG pullback (long_pullback):    trend-following dip-buy
     - EMA50 > EMA200                          (uptrend)
     - close > EMA50                           (still engaged with the trend)
     - RSI(14) dipped below 40 within the last few candles, now recovered to 40-60
     - MACD currently above its signal line (bullish right now)

2. LONG trend continuation (long_breakout):   catches a strong trend with no deep dip
     - EMA50 > EMA200                          (uptrend)
     - close makes a new N-candle high         (breakout)
     - RSI(14) between 50-70                   (healthy momentum, not overbought)
     - MACD > signal line AND MACD > 0         (positive, active momentum)

3. SHORT pullback (short_pullback):  mirror of (1) for downtrends
     - EMA50 < EMA200                          (downtrend)
     - close < EMA50
     - RSI(14) rose above 60 within the last few candles, now back down to 40-60
     - MACD currently below its signal line (bearish right now)

Note on (1)/(3): an earlier version required the RSI cross and the MACD cross to land
on the exact same candle — a 30/180-day backtest (see backtest.py) showed this setup
essentially never fired in practice (both crosses rarely align that precisely). It now
checks "RSI touched the trigger zone recently, MACD currently agrees" instead, which
fired in backtesting while still requiring a genuine pullback-then-recovery pattern.

All three setups additionally require ADX(14) >= 25 — a regime gate. ADX measures
trend STRENGTH, not direction: below ~20 the market is chopping sideways and even a
technically-valid setup above tends to whipsaw, so no entry fires regardless of the
other conditions until the trend is confirmed strong enough to trade.

Risk management (ATR-based, not a fixed %):
    risk  = 1.5 * ATR(14)
    LONG:  SL = entry - risk,  TP1 = entry + 2*risk,  TP2 = entry + 3*risk
    SHORT: SL = entry + risk,  TP1 = entry - 2*risk,  TP2 = entry - 3*risk

Exit alerts (for an already-open position, see position_tracker.py) fire when:
    - price has crossed the position's TP1 / TP2 / SL, or
    - momentum technically reverses against the position: RSI drops back out of
      overbought (>=70 -> <70 for a long, mirrored for a short) OR MACD crosses against
      the position. A stricter version (both signals required together, on a wider RSI
      swing) was backtested and performed WORSE — it let losing trades run further into
      full stop-losses instead of cutting them early, so the looser either/or exit here
      is the one that's actually been validated.

Known backtest caveat (BTC/ETH/SOL/BNB, 180 days, see backtest.py): long_pullback shows
a positive edge (~+0.29R/trade); long_breakout and especially short_pullback have shown
a NEGATIVE edge (~-0.46R and ~-0.50R/trade respectively) in this sample. Small sample,
no fees modeled — treat as a caution flag on shorting, not a proven verdict either way.
"""

from __future__ import annotations

from dataclasses import dataclass

from .indicators import adx, atr, ema, macd, rsi
from .price_data import Candle

EMA_FAST_TREND = 50
EMA_SLOW_TREND = 200
RSI_PERIOD = 14
RSI_RECOVERY_LEVEL = 40  # long pullback trigger
RSI_OVERBOUGHT_CAP = 60
RSI_SHORT_TRIGGER_LEVEL = 60  # short pullback trigger
RSI_OVERSOLD_FLOOR = 40
RSI_CONTINUATION_MIN = 50  # long breakout band
RSI_CONTINUATION_MAX = 70
BREAKOUT_LOOKBACK = 20
PULLBACK_LOOKBACK = 5  # how many recent candles count as "recently dipped/spiked"
ATR_PERIOD = 14
SL_ATR_MULTIPLIER = 1.5
TP1_RISK_MULTIPLE = 2.0
TP2_RISK_MULTIPLE = 3.0
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25  # below this, market regime is too choppy to trade
# A 180-day/5-coin backtest (2026-08-22) showed entries at ADX 25-35 averaged +0.06R/trade
# while ADX 35-45 averaged -0.84R/trade — a very high ADX means the trend is likely
# already extended/near exhaustion, so entering there is buying late, not early.
ADX_TREND_CEILING = 35

# short_pullback backtested consistently negative (~-0.42R/trade) across BTC/ETH/SOL/BNB,
# 180 days, in every variant tested on 2026-08-22 — disabled pending a redesign. The
# detection logic and its exit handling are left in place (dormant) so it's a one-line
# re-enable once revisited, not a rewrite.
SHORT_SIGNALS_ENABLED = False

MIN_CANDLES_REQUIRED = EMA_SLOW_TREND + 10


@dataclass(frozen=True)
class SignalParams:
    """Every tunable number the entry/exit rules use. Defaults are the values the bot
    has always run with (the module constants above), so `compute_entry_signal(...)`
    with no `params` is byte-for-byte the same as before this was added. Passing a
    custom SignalParams lets a caller (e.g. a what-if backtest) test a variant WITHOUT
    changing the live strategy."""

    ema_fast: int = EMA_FAST_TREND
    ema_slow: int = EMA_SLOW_TREND
    rsi_period: int = RSI_PERIOD
    rsi_recovery_level: float = RSI_RECOVERY_LEVEL
    rsi_overbought_cap: float = RSI_OVERBOUGHT_CAP
    rsi_continuation_min: float = RSI_CONTINUATION_MIN
    rsi_continuation_max: float = RSI_CONTINUATION_MAX
    rsi_short_trigger_level: float = RSI_SHORT_TRIGGER_LEVEL
    rsi_oversold_floor: float = RSI_OVERSOLD_FLOOR
    breakout_lookback: int = BREAKOUT_LOOKBACK
    pullback_lookback: int = PULLBACK_LOOKBACK
    atr_period: int = ATR_PERIOD
    sl_atr_multiplier: float = SL_ATR_MULTIPLIER
    tp1_risk_multiple: float = TP1_RISK_MULTIPLE
    tp2_risk_multiple: float = TP2_RISK_MULTIPLE
    adx_period: int = ADX_PERIOD
    adx_trend_threshold: float = ADX_TREND_THRESHOLD
    adx_trend_ceiling: float = ADX_TREND_CEILING
    short_signals_enabled: bool = SHORT_SIGNALS_ENABLED


DEFAULT_PARAMS = SignalParams()


def min_candles_required(params: SignalParams = DEFAULT_PARAMS) -> int:
    """Warm-up length — must cover the slowest EMA. `MIN_CANDLES_REQUIRED` stays as the
    module-level default for existing importers."""
    return params.ema_slow + 10


@dataclass
class TradeSignal:
    symbol: str
    timeframe: str
    direction: str  # "LONG" or "SHORT"
    setup: str  # "long_pullback" | "long_breakout" | "short_pullback"
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    rsi_value: float
    ema_fast: float
    ema_slow: float
    adx_value: float


@dataclass
class ExitEvent:
    symbol: str
    direction: str  # direction of the position being exited
    reason: str  # "tp1_hit" | "tp2_hit" | "sl_hit" | "technical_reversal"
    price: float
    rsi_value: float


class _Indicators:
    """Computes and caches all indicator series for one candle set."""

    def __init__(self, candles: list[Candle], params: SignalParams = DEFAULT_PARAMS):
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        self.closes = closes
        self.highs = highs
        self.ema_fast = ema(closes, params.ema_fast)
        self.ema_slow = ema(closes, params.ema_slow)
        self.rsi = rsi(closes, params.rsi_period)
        self.macd_line, self.signal_line = macd(closes)
        self.atr = atr(highs, lows, closes, params.atr_period)
        self.adx = adx(highs, lows, closes, params.adx_period)

    def ready(self) -> bool:
        series = [self.ema_fast, self.ema_slow, self.rsi, self.macd_line, self.signal_line, self.atr, self.adx]
        return all(series) and len(self.rsi) >= 2 and len(self.macd_line) >= 2 and len(self.signal_line) >= 2


def _risk_targets(
    direction: str, entry: float, atr_now: float, params: SignalParams = DEFAULT_PARAMS
) -> tuple[float, float, float]:
    risk = params.sl_atr_multiplier * atr_now
    if direction == "LONG":
        return entry - risk, entry + params.tp1_risk_multiple * risk, entry + params.tp2_risk_multiple * risk
    return entry + risk, entry - params.tp1_risk_multiple * risk, entry - params.tp2_risk_multiple * risk


def compute_entry_signal(
    symbol: str, timeframe: str, candles: list[Candle], params: SignalParams = DEFAULT_PARAMS
) -> TradeSignal | None:
    if len(candles) < min_candles_required(params):
        return None
    ind = _Indicators(candles, params)
    if not ind.ready():
        return None

    close_now = ind.closes[-1]
    ema_fast_now, ema_slow_now = ind.ema_fast[-1], ind.ema_slow[-1]
    rsi_now = ind.rsi[-1]
    macd_now, sig_now = ind.macd_line[-1], ind.signal_line[-1]
    atr_now = ind.atr[-1]
    adx_now = ind.adx[-1]

    uptrend = ema_fast_now > ema_slow_now
    downtrend = ema_fast_now < ema_slow_now
    trending = params.adx_trend_threshold <= adx_now < params.adx_trend_ceiling  # regime gate — chop or an already-exhausted trend kills all 3 setups below

    lookback = params.pullback_lookback
    recent_rsi_min = min(ind.rsi[-(lookback + 1) : -1]) if len(ind.rsi) > lookback else rsi_now
    recent_rsi_max = max(ind.rsi[-(lookback + 1) : -1]) if len(ind.rsi) > lookback else rsi_now

    # 1. LONG pullback
    if (
        trending
        and uptrend
        and close_now > ema_fast_now
        and recent_rsi_min < params.rsi_recovery_level
        and params.rsi_recovery_level <= rsi_now < params.rsi_overbought_cap
        and macd_now > sig_now
    ):
        sl, tp1, tp2 = _risk_targets("LONG", close_now, atr_now, params)
        return TradeSignal(symbol, timeframe, "LONG", "long_pullback", close_now, sl, tp1, tp2, rsi_now, ema_fast_now, ema_slow_now, adx_now)

    # 2. LONG trend continuation
    prior_high = max(ind.highs[-(params.breakout_lookback + 1) : -1]) if len(ind.highs) > params.breakout_lookback else None
    if (
        trending
        and uptrend
        and prior_high is not None
        and close_now >= prior_high
        and params.rsi_continuation_min <= rsi_now <= params.rsi_continuation_max
        and macd_now > sig_now
        and macd_now > 0
    ):
        sl, tp1, tp2 = _risk_targets("LONG", close_now, atr_now, params)
        return TradeSignal(symbol, timeframe, "LONG", "long_breakout", close_now, sl, tp1, tp2, rsi_now, ema_fast_now, ema_slow_now, adx_now)

    # 3. SHORT pullback (disabled by default — see SignalParams.short_signals_enabled)
    if (
        params.short_signals_enabled
        and trending
        and downtrend
        and close_now < ema_fast_now
        and recent_rsi_max > params.rsi_short_trigger_level
        and params.rsi_oversold_floor < rsi_now <= params.rsi_short_trigger_level
        and macd_now < sig_now
    ):
        sl, tp1, tp2 = _risk_targets("SHORT", close_now, atr_now, params)
        return TradeSignal(symbol, timeframe, "SHORT", "short_pullback", close_now, sl, tp1, tp2, rsi_now, ema_fast_now, ema_slow_now, adx_now)

    return None


def compute_exit_event(
    position: dict, candles: list[Candle], params: SignalParams = DEFAULT_PARAMS
) -> ExitEvent | None:
    """position: dict with symbol, direction, stop_loss, take_profit_1, take_profit_2
    (as stored by position_tracker.py — take_profit_1/stop_loss may have been updated
    after a partial TP1, e.g. moved to breakeven)."""
    if len(candles) < min_candles_required(params):
        return None
    ind = _Indicators(candles, params)
    if not ind.ready():
        return None

    symbol = position["symbol"]
    direction = position["direction"]
    close_now = ind.closes[-1]
    rsi_now, rsi_prev = ind.rsi[-1], ind.rsi[-2]
    macd_now, macd_prev = ind.macd_line[-1], ind.macd_line[-2]
    sig_now, sig_prev = ind.signal_line[-1], ind.signal_line[-2]

    if direction == "LONG":
        if close_now <= position["stop_loss"]:
            return ExitEvent(symbol, direction, "sl_hit", close_now, rsi_now)
        if not position.get("tp1_hit") and close_now >= position["take_profit_1"]:
            return ExitEvent(symbol, direction, "tp1_hit", close_now, rsi_now)
        if close_now >= position["take_profit_2"]:
            return ExitEvent(symbol, direction, "tp2_hit", close_now, rsi_now)
        # Exit early on EITHER signal alone (not requiring both together). A stricter
        # "both signals + wider RSI swing" version was backtested and let losers run
        # further into full stop-losses instead of cutting them early — this looser,
        # faster-to-react version backtested better across BTC/ETH/SOL/BNB.
        if (rsi_prev >= params.rsi_overbought_cap + 10 > rsi_now) or (macd_prev >= sig_prev and macd_now < sig_now):
            return ExitEvent(symbol, direction, "technical_reversal", close_now, rsi_now)
    else:  # SHORT
        if close_now >= position["stop_loss"]:
            return ExitEvent(symbol, direction, "sl_hit", close_now, rsi_now)
        if not position.get("tp1_hit") and close_now <= position["take_profit_1"]:
            return ExitEvent(symbol, direction, "tp1_hit", close_now, rsi_now)
        if close_now <= position["take_profit_2"]:
            return ExitEvent(symbol, direction, "tp2_hit", close_now, rsi_now)
        if (rsi_prev <= params.rsi_oversold_floor - 10 < rsi_now) or (macd_prev <= sig_prev and macd_now > sig_now):
            return ExitEvent(symbol, direction, "technical_reversal", close_now, rsi_now)

    return None
