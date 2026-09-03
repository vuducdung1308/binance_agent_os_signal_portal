"""The only module that reaches into the existing bot codebase.

It puts BOT_ROOT on sys.path and re-exports the bot's *unchanged* functions:

  - src.price_data.fetch_klines        (closed candles, oldest-first)
  - src.signal_engine.compute_entry_signal / compute_exit_event / MIN_CANDLES_REQUIRED
  - src.indicators.{ema,rsi,macd,atr,adx}
  - src.price_watch.fetch_current_price
  - src.order_flow.get_order_flow_snapshot

Nothing here mutates bot state. The bot's own data/open_positions.json is read
read-only for display; the portal keeps its own paper positions in SQLite.

`indicator_snapshot()` is the one piece of new logic: it computes the current value
of every indicator the bot uses, for display even when no trade signal fires. It calls
the bot's public src/indicators.py functions directly (same math the engine's private
_Indicators helper runs) so it stays correct if the engine internals change.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .settings import load_portal_settings

_S = load_portal_settings()
if str(_S.bot_root) not in sys.path:
    sys.path.insert(0, str(_S.bot_root))

# Make the bot's own .env visible (SIGNAL_TIMEFRAME etc.) before importing src.config.
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv(_S.bot_root / ".env")
except Exception:
    pass

from src.indicators import adx, atr, ema, macd, rsi  # noqa: E402
from src.price_data import Candle, fetch_klines  # noqa: E402
from src.price_watch import fetch_current_price  # noqa: E402
from src.signal_engine import (  # noqa: E402
    MIN_CANDLES_REQUIRED,
    ADX_TREND_CEILING,
    ADX_TREND_THRESHOLD,
    BREAKOUT_LOOKBACK,
    DEFAULT_PARAMS,
    EMA_FAST_TREND,
    EMA_SLOW_TREND,
    PULLBACK_LOOKBACK,
    RSI_CONTINUATION_MAX,
    RSI_CONTINUATION_MIN,
    RSI_OVERBOUGHT_CAP,
    RSI_OVERSOLD_FLOOR,
    RSI_PERIOD,
    RSI_RECOVERY_LEVEL,
    RSI_SHORT_TRIGGER_LEVEL,
    SHORT_SIGNALS_ENABLED,
    SignalParams,
    compute_entry_signal,
    compute_exit_event,
)

try:
    from src.order_flow import get_order_flow_snapshot  # noqa: E402
except Exception:  # order_flow pulls extra endpoints; treat as optional
    get_order_flow_snapshot = None  # type: ignore

try:
    import backtest as _bot_backtest  # noqa: E402  (script at the bot root)
except Exception:
    _bot_backtest = None  # type: ignore

BOT_OPEN_POSITIONS_FILE = _S.bot_root / "data" / "open_positions.json"
BOT_SIGNALS_DIR = _S.bot_root / "output" / "signals"

__all__ = [
    "Candle",
    "fetch_klines",
    "fetch_current_price",
    "compute_entry_signal",
    "compute_exit_event",
    "get_order_flow_snapshot",
    "indicator_snapshot",
    "engine_diagnostics",
    "analyze",
    "run_backtest",
    "param_defaults",
    "clean_param_overrides",
    "PARAMS_META",
    "signal_to_dict",
    "read_bot_open_positions",
    "MIN_CANDLES_REQUIRED",
    "ENGINE_DEFAULTS",
]

# Surfaced to the frontend so the config screen can show what the *engine* uses vs.
# the portal's own display thresholds (the engine ones are not editable here).
ENGINE_DEFAULTS = {
    "ema_fast": EMA_FAST_TREND,
    "ema_slow": EMA_SLOW_TREND,
    "rsi_period": RSI_PERIOD,
    "adx_trend_min": ADX_TREND_THRESHOLD,
    "adx_trend_max": ADX_TREND_CEILING,
}


def _compute(candles: List[Candle]) -> Optional[Dict[str, Any]]:
    """All indicator series + scalars the portal needs, computed once. None if the
    history is too short. CLOSED candles only — same basis the bot's signals use."""
    if len(candles) < MIN_CANDLES_REQUIRED:
        return None

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]

    ema_fast = ema(closes, EMA_FAST_TREND)
    ema_slow = ema(closes, EMA_SLOW_TREND)
    rsi_series = rsi(closes, RSI_PERIOD)
    macd_line, signal_line = macd(closes)
    atr_series = atr(highs, lows, closes)
    adx_series = adx(highs, lows, closes)

    if not (ema_fast and ema_slow and rsi_series and macd_line and signal_line and atr_series and adx_series):
        return None
    if len(rsi_series) < PULLBACK_LOOKBACK + 1 or len(highs) < BREAKOUT_LOOKBACK + 1:
        return None

    return {
        "closes": closes, "highs": highs, "lows": lows, "vols": vols,
        "ema_fast": ema_fast, "ema_slow": ema_slow, "rsi": rsi_series,
        "macd_line": macd_line, "signal_line": signal_line,
        "atr": atr_series, "adx": adx_series, "candles": candles,
    }


def _snapshot(s: Dict[str, Any]) -> Dict[str, Any]:
    closes, vols = s["closes"], s["vols"]
    macd_now, macd_sig = s["macd_line"][-1], s["signal_line"][-1]
    vol_now = vols[-1]
    vol_avg20 = sum(vols[-20:]) / min(len(vols), 20)
    ef, es = s["ema_fast"][-1], s["ema_slow"][-1]
    return {
        "price": closes[-1],
        "candle_open_time": s["candles"][-1].open_time,
        "candle_close_time": s["candles"][-1].close_time,
        "ema_fast": ef,
        "ema_slow": es,
        "trend": "up" if ef > es else "down",
        "rsi": s["rsi"][-1],
        "macd": macd_now,
        "macd_signal": macd_sig,
        "macd_hist": macd_now - macd_sig,
        "atr": s["atr"][-1],
        "adx": s["adx"][-1],
        "vol": vol_now,
        "vol_avg20": vol_avg20,
        "vol_ratio": (vol_now / vol_avg20) if vol_avg20 else 0.0,
    }


def _diagnostics(s: Dict[str, Any]) -> Dict[str, Any]:
    """Which of the bot engine's entry conditions currently pass / fail, per setup.
    A read-only mirror of the gates in src/signal_engine.compute_entry_signal — it
    imports that module's constants, it does not re-tune anything."""
    closes, highs, rsi_s = s["closes"], s["highs"], s["rsi"]
    ef, es = s["ema_fast"][-1], s["ema_slow"][-1]
    close_now = closes[-1]
    rsi_now = rsi_s[-1]
    macd_now, sig_now = s["macd_line"][-1], s["signal_line"][-1]
    adx_now = s["adx"][-1]

    uptrend = ef > es
    downtrend = ef < es
    trending = ADX_TREND_THRESHOLD <= adx_now < ADX_TREND_CEILING
    recent_rsi_min = min(rsi_s[-(PULLBACK_LOOKBACK + 1):-1])
    recent_rsi_max = max(rsi_s[-(PULLBACK_LOOKBACK + 1):-1])
    prior_high = max(highs[-(BREAKOUT_LOOKBACK + 1):-1])

    def c(label: str, ok: bool, detail: str = "") -> dict:
        return {"label": label, "ok": bool(ok), "detail": detail}

    def g4(v: float) -> str:
        if abs(v) >= 1000:
            return f"{v:,.0f}"
        if abs(v) >= 1:
            return f"{v:,.4f}"
        return f"{v:.6f}"
    regime = c(f"ADX in {ADX_TREND_THRESHOLD}-{ADX_TREND_CEILING}", trending, f"ADX {adx_now:.1f}")

    long_pullback = [
        regime,
        c("Uptrend (EMA50>EMA200)", uptrend, f"{g4(ef)} / {g4(es)}"),
        c("Price above EMA50", close_now > ef, f"{g4(close_now)} / {g4(ef)}"),
        c(f"RSI dipped <{RSI_RECOVERY_LEVEL} recently", recent_rsi_min < RSI_RECOVERY_LEVEL,
          f"recent low {recent_rsi_min:.1f}"),
        c(f"RSI now {RSI_RECOVERY_LEVEL}-{RSI_OVERBOUGHT_CAP}",
          RSI_RECOVERY_LEVEL <= rsi_now < RSI_OVERBOUGHT_CAP, f"RSI {rsi_now:.1f}"),
        c("MACD > signal", macd_now > sig_now, f"{g4(macd_now)} / {g4(sig_now)}"),
    ]
    long_breakout = [
        regime,
        c("Uptrend (EMA50>EMA200)", uptrend, f"{g4(ef)} / {g4(es)}"),
        c(f"Breaks {BREAKOUT_LOOKBACK}-candle high", close_now >= prior_high,
          f"{g4(close_now)} / high {g4(prior_high)}"),
        c(f"RSI {RSI_CONTINUATION_MIN}-{RSI_CONTINUATION_MAX}",
          RSI_CONTINUATION_MIN <= rsi_now <= RSI_CONTINUATION_MAX, f"RSI {rsi_now:.1f}"),
        c("MACD > signal", macd_now > sig_now, f"{g4(macd_now)} / {g4(sig_now)}"),
        c("MACD > 0", macd_now > 0, g4(macd_now)),
    ]
    setups = [
        {"name": "long_pullback", "enabled": True, "conditions": long_pullback},
        {"name": "long_breakout", "enabled": True, "conditions": long_breakout},
    ]
    if SHORT_SIGNALS_ENABLED:
        setups.append({"name": "short_pullback", "enabled": True, "conditions": [
            regime,
            c("Downtrend (EMA50<EMA200)", downtrend, f"{g4(ef)} / {g4(es)}"),
            c("Price below EMA50", close_now < ef, f"{g4(close_now)} / {g4(ef)}"),
            c(f"RSI spiked >{RSI_SHORT_TRIGGER_LEVEL} recently", recent_rsi_max > RSI_SHORT_TRIGGER_LEVEL,
              f"recent high {recent_rsi_max:.1f}"),
            c(f"RSI now {RSI_OVERSOLD_FLOOR}-{RSI_SHORT_TRIGGER_LEVEL}",
              RSI_OVERSOLD_FLOOR < rsi_now <= RSI_SHORT_TRIGGER_LEVEL, f"RSI {rsi_now:.1f}"),
            c("MACD < signal", macd_now < sig_now, f"{g4(macd_now)} / {g4(sig_now)}"),
        ]})
    else:
        setups.append({"name": "short_pullback", "enabled": False, "conditions": []})

    for st in setups:
        if st["enabled"] and st["conditions"]:
            st["match"] = all(x["ok"] for x in st["conditions"])
            st["missing"] = [x["label"] for x in st["conditions"] if not x["ok"]]
        else:
            st["match"] = False
            st["missing"] = None

    active = [st for st in setups if st["enabled"] and st["conditions"]]
    closest = min(active, key=lambda st: len(st["missing"])) if active else None
    return {
        "setups": setups,
        "closest": closest["name"] if closest else None,
        "closest_missing": closest["missing"] if closest else None,
        "any_match": any(st["match"] for st in setups),
    }


def analyze(candles: List[Candle]) -> Optional[Dict[str, Any]]:
    """Snapshot + engine-condition diagnostics in one pass (series computed once)."""
    s = _compute(candles)
    if s is None:
        return None
    return {"snapshot": _snapshot(s), "diagnostics": _diagnostics(s)}


def indicator_snapshot(candles: List[Candle]) -> Optional[Dict[str, Any]]:
    s = _compute(candles)
    return _snapshot(s) if s is not None else None


def engine_diagnostics(candles: List[Candle]) -> Optional[Dict[str, Any]]:
    s = _compute(candles)
    return _diagnostics(s) if s is not None else None


def signal_to_dict(sig) -> Dict[str, Any]:
    return asdict(sig)


def read_bot_open_positions() -> Dict[str, dict]:
    """The bot's live paper positions (data/open_positions.json), read-only."""
    try:
        if BOT_OPEN_POSITIONS_FILE.exists():
            return json.loads(BOT_OPEN_POSITIONS_FILE.read_text()) or {}
    except Exception:
        pass
    return {}


# Which SignalParams fields the portal lets you tune, with UI hints. "primary" =
# always shown, "advanced" = behind a collapse. Everything here maps 1:1 to a
# SignalParams field; nothing else in the engine is touched.
PARAMS_META = [
    {"key": "adx_trend_threshold", "label": "ADX minimum (regime gate)", "type": "float", "group": "primary"},
    {"key": "adx_trend_ceiling", "label": "ADX maximum (above = trend likely exhausted)", "type": "float", "group": "primary"},
    {"key": "rsi_recovery_level", "label": "RSI recovery — long pullback (dipped below this recently)", "type": "float", "group": "primary"},
    {"key": "rsi_overbought_cap", "label": "RSI cap for a long-pullback entry", "type": "float", "group": "primary"},
    {"key": "rsi_continuation_min", "label": "RSI min — long breakout", "type": "float", "group": "primary"},
    {"key": "rsi_continuation_max", "label": "RSI max — long breakout", "type": "float", "group": "primary"},
    {"key": "sl_atr_multiplier", "label": "SL = n × ATR", "type": "float", "group": "primary"},
    {"key": "tp1_risk_multiple", "label": "TP1 = n × risk", "type": "float", "group": "primary"},
    {"key": "tp2_risk_multiple", "label": "TP2 = n × risk", "type": "float", "group": "primary"},
    {"key": "breakout_lookback", "label": "Candles for the breakout high", "type": "int", "group": "primary"},
    {"key": "pullback_lookback", "label": "Candles counting as 'just dipped / bounced'", "type": "int", "group": "primary"},
    {"key": "ema_fast", "label": "Fast EMA (trend)", "type": "int", "group": "advanced"},
    {"key": "ema_slow", "label": "Slow EMA (trend)", "type": "int", "group": "advanced"},
    {"key": "rsi_period", "label": "RSI period", "type": "int", "group": "advanced"},
    {"key": "atr_period", "label": "ATR period", "type": "int", "group": "advanced"},
    {"key": "adx_period", "label": "ADX period", "type": "int", "group": "advanced"},
    {"key": "rsi_short_trigger_level", "label": "RSI SHORT trigger", "type": "float", "group": "advanced"},
    {"key": "rsi_oversold_floor", "label": "RSI SHORT floor", "type": "float", "group": "advanced"},
    {"key": "short_signals_enabled", "label": "Enable SHORT signals", "type": "bool", "group": "advanced"},
]
_PARAM_TYPES = {m["key"]: m["type"] for m in PARAMS_META}


def param_defaults() -> Dict[str, Any]:
    return asdict(DEFAULT_PARAMS)


def clean_param_overrides(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only known keys, coerce to the right type, drop anything equal to default."""
    if not raw:
        return {}
    defaults = asdict(DEFAULT_PARAMS)
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if k not in _PARAM_TYPES or v is None or v == "":
            continue
        try:
            if _PARAM_TYPES[k] == "int":
                cv: Any = int(round(float(v)))
            elif _PARAM_TYPES[k] == "bool":
                cv = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            else:
                cv = float(v)
        except (TypeError, ValueError):
            continue
        if cv != defaults[k]:
            out[k] = cv
    return out


def _bt_summary(trades: list, symbol: str, timeframe: str, days: int) -> Dict[str, Any]:
    rows = [
        {
            "setup": t.setup, "direction": t.direction, "entry_price": t.entry_price,
            "r_multiple": round(t.r_multiple, 3), "entry_rsi": round(t.entry_rsi, 1),
            "entry_adx": round(t.entry_adx, 1), "hold_candles": t.hold_candles,
            "exit_reasons": t.exit_reasons,
        }
        for t in trades
    ]
    if not trades:
        return {"symbol": symbol, "timeframe": timeframe, "days": days,
                "trades": 0, "rows": [], "by_setup": {}}

    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple <= 0]
    total_r = sum(t.r_multiple for t in trades)
    gross_win = sum(t.r_multiple for t in wins)
    gross_loss = abs(sum(t.r_multiple for t in losses))

    equity = peak = max_dd = 0.0
    for t in trades:
        equity += t.r_multiple
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    by_setup: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        b = by_setup.setdefault(t.setup, {"trades": 0, "wins": 0, "total_r": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if t.r_multiple > 0 else 0
        b["total_r"] = round(b["total_r"] + t.r_multiple, 3)

    return {
        "symbol": symbol, "timeframe": timeframe, "days": days,
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_r": round(total_r, 2),
        "avg_r": round(total_r / len(trades), 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown_r": round(max_dd, 2),
        "by_setup": by_setup, "rows": rows,
    }


def run_backtest(
    symbol: str, timeframe: str, days: int,
    param_overrides: Optional[Dict[str, Any]] = None, compare: bool = True,
) -> Dict[str, Any]:
    """Replays the bot's exact production functions over history via its own
    backtest.py (no reimplementation). If param_overrides is given, runs with a tuned
    SignalParams and (when compare) also with the engine defaults so the UI can show
    both side by side. Blocking (paginates klines history) — call from a thread."""
    if _bot_backtest is None:
        raise RuntimeError("bot backtest module unavailable")

    overrides = clean_param_overrides(param_overrides)
    params = SignalParams(**overrides) if overrides else DEFAULT_PARAMS

    tuned = _bt_summary(_bot_backtest.run_backtest(symbol, timeframe, days, params),
                        symbol, timeframe, days)
    out: Dict[str, Any] = {
        "tuned": tuned,
        "overrides": overrides,
        "params_used": asdict(params),
        "defaults": asdict(DEFAULT_PARAMS),
        "default": None,
    }
    if compare and overrides:
        out["default"] = _bt_summary(
            _bot_backtest.run_backtest(symbol, timeframe, days, DEFAULT_PARAMS),
            symbol, timeframe, days,
        )
    return out
