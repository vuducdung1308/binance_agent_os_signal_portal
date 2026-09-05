"""Background workers + the WebSocket broadcast hub.

Two independent cadences (see settings.py for the reasoning):

  price loop   (fast, ~5s)  -> one batched 24hr-ticker request for the whole watchlist,
                               broadcast {type: "price"} rows. Cheap, gives the live feel.
  signal loop  (~30s)       -> per symbol: fetch CLOSED candles via the bot's fetch_klines,
                               recompute every indicator, run the bot's compute_entry_signal
                               / compute_exit_event unchanged, update the portal's own paper
                               positions, persist snapshots, broadcast {type: "indicators"}
                               and {type: "signal"}.

Nothing here writes to the bot's files.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set

from . import bot_bridge as bb
from .market import ticker_24hr
from .notify import Telegram, format_signal
from .settings import PortalSettings
from .store import Store

log = logging.getLogger("portal.poller")

SPARK_MAX = 240  # points kept per symbol for the card sparkline


class Hub:
    """Fan-out to every connected WebSocket. Also keeps the last message of each
    (type, symbol) so a freshly-connected client can be primed immediately."""

    def __init__(self) -> None:
        self._clients: Set[Any] = set()
        self._lock = asyncio.Lock()
        self.last_price: Dict[str, dict] = {}
        self.last_indicators: Dict[str, dict] = {}
        self.spark: Dict[str, Deque[dict]] = {}

    def seed_spark(self, store: Store) -> None:
        """Prime each symbol's sparkline buffer from stored snapshots (downsampled)."""
        for sym in store.watchlist_symbols():
            rows = store.snapshots(sym, since_hours=24, limit=10000)
            dq: Deque[dict] = collections.deque(maxlen=SPARK_MAX)
            step = max(1, len(rows) // 120)
            for r in rows[::step]:
                try:
                    t = int(datetime.fromisoformat(r["ts"]).timestamp())
                except (ValueError, TypeError):
                    continue
                dq.append({"t": t, "price": r["price"], "rsi": r["rsi"]})
            self.spark[sym] = dq

    def push_spark(self, sym: str, point: dict) -> None:
        dq = self.spark.get(sym)
        if dq is None:
            dq = collections.deque(maxlen=SPARK_MAX)
            self.spark[sym] = dq
        dq.append(point)

    async def register(self, ws) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message, default=_json_default)
        async with self._lock:
            dead = []
            for ws in self._clients:
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)

    def prime_payload(self) -> dict:
        return {
            "type": "prime",
            "prices": list(self.last_price.values()),
            "indicators": list(self.last_indicators.values()),
        }


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not serializable: {type(o)}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate_alerts(snap: Dict[str, Any], cfg: Dict[str, float]) -> List[dict]:
    """Portal-only display alerts derived from the thresholds table. Does NOT affect
    the bot's signal engine — purely badges/colours for the dashboard."""
    alerts: List[dict] = []
    rsi = snap.get("rsi")
    adx = snap.get("adx")
    hist = snap.get("macd_hist")
    vr = snap.get("vol_ratio")

    if rsi is not None:
        if rsi >= cfg["rsi_overbought"]:
            alerts.append({"level": "bad", "text": f"RSI overbought {rsi:.0f}"})
        elif rsi <= cfg["rsi_oversold"]:
            alerts.append({"level": "good", "text": f"RSI oversold {rsi:.0f}"})

    if adx is not None:
        if adx < cfg["adx_min"]:
            alerts.append({"level": "muted", "text": f"No trend (ADX {adx:.0f})"})
        elif adx >= cfg["adx_max"]:
            alerts.append({"level": "warn", "text": f"Trend overextended (ADX {adx:.0f})"})
        else:
            alerts.append({"level": "info", "text": f"Trade-zone ADX {adx:.0f}"})

    if hist is not None:
        if hist > cfg["macd_hist_min"]:
            alerts.append({"level": "good", "text": "MACD bullish"})
        else:
            alerts.append({"level": "warn", "text": "MACD bearish"})

    if vr is not None and vr >= cfg["vol_ratio_min"]:
        alerts.append({"level": "warn", "text": f"Volume spike x{vr:.1f}"})

    return alerts


class Poller:
    def __init__(self, settings: PortalSettings, store: Store, hub: Hub) -> None:
        self.s = settings
        self.store = store
        self.hub = hub
        self._tasks: List[asyncio.Task] = []
        self._last_snapshot_at: Dict[str, float] = {}
        self.telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
        # First signal-loop pass only seeds state — don't Telegram the backlog. Armed
        # once the first full pass finishes; genuinely new signals after that notify.
        self._notify_armed = False

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._price_loop(), name="portal-price-loop"),
            asyncio.create_task(self._signal_loop(), name="portal-signal-loop"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

    # ---------------- price loop ----------------
    async def _price_loop(self) -> None:
        while True:
            try:
                symbols = self.store.watchlist_symbols()
                if symbols:
                    tick = await asyncio.to_thread(ticker_24hr, symbols)
                    rows = []
                    for sym, t in tick.items():
                        row = {"type": "price", "symbol": sym, "ts": _now(), **t}
                        self.hub.last_price[sym] = row
                        rows.append(row)
                    if rows:
                        await self.hub.broadcast({"type": "price_batch", "rows": rows, "ts": _now()})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("price loop error")
            await asyncio.sleep(self.s.price_poll_sec)

    # ---------------- signal loop ----------------
    async def _signal_loop(self) -> None:
        # small initial delay so the price loop primes first
        await asyncio.sleep(1)
        while True:
            try:
                await self._ingest_bot_signals()
                symbols = self.store.watchlist_symbols()
                bot_positions = bb_read_bot_positions()
                for sym in symbols:
                    try:
                        await self._process_symbol(sym, bot_positions.get(sym))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("signal loop error for %s", sym)
                    await asyncio.sleep(0.15)  # gentle pacing between symbols
                if not self._notify_armed:
                    self._notify_armed = True
                    if self.telegram.enabled:
                        log.info("Telegram notifications armed (chat %s)", self.s.telegram_chat_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("signal loop error")
            await asyncio.sleep(self.s.signal_poll_sec)

    async def _emit_signal(self, ev: dict) -> None:
        """Broadcast a new signal to the browsers and, when configured, to Telegram."""
        await self.hub.broadcast({"type": "signal", "event": ev, "ts": _now()})
        if not (self._notify_armed and self.telegram.enabled):
            return
        if ev.get("source") == "bot" and not self.s.telegram_include_bot_signals:
            return
        try:
            await asyncio.to_thread(self.telegram.send, format_signal(ev))
        except Exception:
            log.exception("telegram notify failed")

    async def _ingest_bot_signals(self) -> None:
        """Pick up entries/exits the bot itself wrote to output/signals/*.txt after the
        portal started (the one-time startup backfill only covers files that existed
        then). Keeps each coin's 'latest signal' and the history in step with what the
        bot actually did — e.g. a live SL_HIT the bot's own watcher fired."""
        try:
            from .backfill import parse_signals_dir

            rows = await asyncio.to_thread(parse_signals_dir, bb.BOT_SIGNALS_DIR)
            new = await asyncio.to_thread(self.store.insert_bot_signals, rows)
        except Exception:
            log.exception("bot-signal ingest failed")
            return
        for ev in new:
            await self._emit_signal(ev)

    async def _process_symbol(self, symbol: str, bot_position: Optional[dict]) -> None:
        candles = await asyncio.to_thread(
            bb.fetch_klines, symbol, self.s.timeframe, self.s.klines_limit
        )
        analysis = await asyncio.to_thread(bb.analyze, candles)
        if analysis is None:
            return
        snap = analysis["snapshot"]
        diagnostics = analysis["diagnostics"]

        spark_point = {"t": int(time.time()), "price": snap["price"], "rsi": snap["rsi"]}
        self.hub.push_spark(symbol, spark_point)

        cfg = self.store.resolved_config_for(symbol)
        alerts = evaluate_alerts(snap, cfg)

        # --- entry / exit via the bot's unchanged engine ---
        engine_signal = await asyncio.to_thread(
            bb.compute_entry_signal, symbol, self.s.timeframe, candles
        )
        portal_pos = self.store.get_portal_position(symbol)
        new_events: List[dict] = []

        if portal_pos:
            exit_ev = await asyncio.to_thread(bb.compute_exit_event, portal_pos, candles)
            if exit_ev is not None:
                ev = self._record_exit(symbol, exit_ev, portal_pos, snap)
                if ev:
                    new_events.append(ev)
                if exit_ev.reason == "tp1_hit":
                    self.store.portal_position_breakeven(symbol)
                else:
                    self.store.close_portal_position(symbol)
        elif engine_signal is not None:
            sig = bb.signal_to_dict(engine_signal)
            ev = self._record_entry(symbol, sig, snap)
            if ev:
                new_events.append(ev)
            self.store.open_portal_position(sig)

        # --- persist a snapshot row at the slower snapshot cadence ---
        # A DB hiccup here (locked / transiently readonly) must NOT stop the live
        # broadcast below — history is best-effort, the dashboard is not.
        loop_now = asyncio.get_event_loop().time()
        if loop_now - self._last_snapshot_at.get(symbol, 0) >= self.s.snapshot_interval_sec:
            try:
                await asyncio.to_thread(self.store.insert_snapshot, symbol, snap)
                self._last_snapshot_at[symbol] = loop_now
            except Exception as exc:
                log.warning("snapshot write failed for %s: %s", symbol, exc)

        latest_entry = self.store.latest_signal(symbol, kind="entry")
        latest_exit = self.store.latest_signal(symbol, kind="exit")
        payload = {
            "type": "indicators",
            "symbol": symbol,
            "ts": _now(),
            "snapshot": snap,
            "diagnostics": diagnostics,
            "spark_point": spark_point,
            "alerts": alerts,
            "engine_signal": bb.signal_to_dict(engine_signal) if engine_signal else None,
            "portal_position": self.store.get_portal_position(symbol),
            "bot_position": bot_position,
            "latest_entry": latest_entry,
            "latest_exit": latest_exit,
        }
        self.hub.last_indicators[symbol] = payload
        await self.hub.broadcast(payload)
        for ev in new_events:
            await self._emit_signal(ev)

    # ---------- signal_event dedup + insert ----------
    def _record_entry(self, symbol: str, sig: dict, snap: dict) -> Optional[dict]:
        last = self.store.latest_signal(symbol)
        if (
            last
            and last["kind"] == "entry"
            and last["direction"] == sig["direction"]
            and last["setup"] == sig["setup"]
            and _fresh(last["last_seen"], self.s.timeframe)
        ):
            self.store.touch_signal(last["id"])
            return None
        eid = self.store.insert_signal_event(
            symbol=symbol, kind="entry", direction=sig["direction"], setup=sig["setup"],
            price=sig["entry"], entry=sig["entry"], stop_loss=sig["stop_loss"],
            take_profit_1=sig["take_profit_1"], take_profit_2=sig["take_profit_2"],
            rsi=snap["rsi"], macd=snap["macd"], macd_signal=snap["macd_signal"],
            adx=snap["adx"], ema_fast=snap["ema_fast"], ema_slow=snap["ema_slow"],
        )
        return self.store.latest_signal(symbol) if eid else None

    def _record_exit(self, symbol: str, exit_ev, position: dict, snap: dict) -> Optional[dict]:
        last = self.store.latest_signal(symbol, kind="exit")
        if (
            last
            and last["setup"] == exit_ev.reason
            and _fresh(last["last_seen"], self.s.timeframe)
        ):
            self.store.touch_signal(last["id"])
            return None
        self.store.insert_signal_event(
            symbol=symbol, kind="exit", direction=position["direction"], setup=exit_ev.reason,
            price=exit_ev.price, entry=position["entry"], stop_loss=position["stop_loss"],
            take_profit_1=position["take_profit_1"], take_profit_2=position["take_profit_2"],
            rsi=snap["rsi"], macd=snap["macd"], macd_signal=snap["macd_signal"],
            adx=snap["adx"], ema_fast=snap["ema_fast"], ema_slow=snap["ema_slow"],
        )
        return self.store.latest_signal(symbol, kind="exit")


def _fresh(iso_ts: str, timeframe: str) -> bool:
    """True if iso_ts is within ~3 candle-widths of now — used to treat a still-firing
    condition as 'the same signal' rather than logging a duplicate every poll."""
    seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
               "2h": 7200, "4h": 14400, "1d": 86400}.get(timeframe, 3600)
    try:
        then = datetime.fromisoformat(iso_ts)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - then).total_seconds() < 3 * seconds


def bb_read_bot_positions() -> Dict[str, dict]:
    try:
        return bb.read_bot_open_positions()
    except Exception:
        return {}
