"""SQLite persistence for the portal: watchlist, display-alert thresholds, detected
signal history, indicator snapshots, and the portal's own paper positions.

Single local user -> one shared connection guarded by a lock is enough.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()

# Portal-side display thresholds (decision: the bot's signal engine is left untouched;
# these only drive colours / badges / the alerts list in the UI).
DEFAULT_ALERT_CONFIG = {
    "rsi_overbought": 70.0,
    "rsi_oversold": 30.0,
    "adx_min": 25.0,
    "adx_max": 35.0,
    "macd_hist_min": 0.0,
    "vol_ratio_min": 2.0,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol      TEXT PRIMARY KEY,
    base        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_config (
    scope   TEXT NOT NULL,          -- 'default' or a symbol e.g. 'BTCUSDT'
    key     TEXT NOT NULL,
    value   REAL NOT NULL,
    PRIMARY KEY (scope, key)
);

-- Saved SignalParams overrides for the what-if backtest (and nothing else — the live
-- poller keeps using engine defaults unless a future opt-in wires these in).
CREATE TABLE IF NOT EXISTS engine_params (
    scope   TEXT NOT NULL,          -- 'default' or a symbol
    key     TEXT NOT NULL,          -- a SignalParams field name
    value   REAL NOT NULL,          -- bools stored as 0/1
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS signal_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    ts          TEXT NOT NULL,      -- first detected (ISO, UTC)
    last_seen   TEXT NOT NULL,      -- still-active, refreshed each poll
    kind        TEXT NOT NULL,      -- 'entry' | 'exit'
    direction   TEXT,              -- LONG | SHORT
    setup       TEXT,              -- long_pullback | long_breakout | tp1_hit | sl_hit | ...
    price       REAL,
    entry       REAL,
    stop_loss   REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    rsi         REAL,
    macd        REAL,
    macd_signal REAL,
    adx         REAL,
    ema_fast    REAL,
    ema_slow    REAL,
    source      TEXT NOT NULL DEFAULT 'portal'   -- 'portal' | 'backfill'
);
CREATE INDEX IF NOT EXISTS idx_sig_symbol_ts ON signal_event(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS indicator_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    price       REAL, rsi REAL, macd REAL, macd_signal REAL, macd_hist REAL,
    adx         REAL, ema_fast REAL, ema_slow REAL, atr REAL,
    vol         REAL, vol_avg20 REAL, vol_ratio REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_symbol_ts ON indicator_snapshot(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS portal_position (
    symbol        TEXT PRIMARY KEY,
    direction     TEXT NOT NULL,
    setup         TEXT NOT NULL,
    entry         REAL NOT NULL,
    stop_loss     REAL NOT NULL,
    take_profit_1 REAL NOT NULL,
    take_profit_2 REAL NOT NULL,
    tp1_hit       INTEGER NOT NULL DEFAULT 0,
    opened_at     TEXT NOT NULL
);

-- ================= live trading (spot, via the Binance Agent OS MCP) =================
-- A single mutable row of day-scoped counters + the manual kill switch.
CREATE TABLE IF NOT EXISTS trading_state (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    day                     TEXT NOT NULL,           -- UTC YYYY-MM-DD the counters belong to
    killed                  INTEGER NOT NULL DEFAULT 0,
    daily_loss_tripped      INTEGER NOT NULL DEFAULT 0,
    orders_today            INTEGER NOT NULL DEFAULT 0,
    realized_pnl_today_usdt REAL NOT NULL DEFAULT 0
);

-- One open spot position per symbol (mirrors what the portal itself opened).
CREATE TABLE IF NOT EXISTS live_position (
    symbol         TEXT PRIMARY KEY,
    mode           TEXT NOT NULL,          -- 'dry-run' | 'live'
    base_qty       REAL NOT NULL,          -- base asset held
    entry_px       REAL NOT NULL,          -- effective fill (estimate in dry-run)
    quote_spent    REAL NOT NULL,          -- USDT committed
    stop_loss      REAL,
    take_profit    REAL,
    open_order_id  TEXT,
    opened_at      TEXT NOT NULL
);

-- Closed round-trips, for realised P/L history.
CREATE TABLE IF NOT EXISTS live_trade (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL,
    mode           TEXT NOT NULL,
    base_qty       REAL NOT NULL,
    entry_px       REAL NOT NULL,
    exit_px        REAL NOT NULL,
    quote_in       REAL NOT NULL,          -- USDT spent opening
    quote_out      REAL NOT NULL,          -- USDT received closing
    realized_pnl   REAL NOT NULL,          -- quote_out - quote_in
    open_order_id  TEXT,
    close_order_id TEXT,
    opened_at      TEXT NOT NULL,
    closed_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_trade_closed ON live_trade(closed_at DESC);

-- Every execution-agent call, for audit.
CREATE TABLE IF NOT EXISTS agent_call (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    intent      TEXT NOT NULL,             -- 'OPEN' | 'CLOSE'
    symbol      TEXT NOT NULL,
    mode        TEXT NOT NULL,
    status      TEXT NOT NULL,             -- dry-run|executed|no-op|blocked|refused|error
    model       TEXT,
    stop_reason TEXT,
    tool_calls  TEXT,                      -- JSON
    text        TEXT,
    error       TEXT,
    guardrail   TEXT                       -- JSON {ok, reasons[]}
);
CREATE INDEX IF NOT EXISTS idx_agent_call_ts ON agent_call(ts DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with _LOCK:
            self.db.executescript(_SCHEMA)
            self.db.commit()

    # ---------- watchlist ----------
    def get_watchlist(self) -> List[dict]:
        with _LOCK:
            rows = self.db.execute(
                "SELECT symbol, base, sort_order FROM watchlist ORDER BY sort_order, symbol"
            ).fetchall()
        return [dict(r) for r in rows]

    def watchlist_symbols(self) -> List[str]:
        return [r["symbol"] for r in self.get_watchlist()]

    def add_watch(self, base: str) -> dict:
        base = base.strip().upper()
        symbol = base if base.endswith("USDT") else f"{base}USDT"
        base = symbol[:-4]
        with _LOCK:
            existing = self.db.execute(
                "SELECT MAX(sort_order) AS m FROM watchlist"
            ).fetchone()["m"]
            nxt = (existing or 0) + 1
            self.db.execute(
                "INSERT OR IGNORE INTO watchlist(symbol, base, sort_order, added_at) VALUES (?,?,?,?)",
                (symbol, base, nxt, _now()),
            )
            self.db.commit()
        return {"symbol": symbol, "base": base}

    def remove_watch(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        with _LOCK:
            self.db.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
            self.db.commit()

    def seed_watchlist(self, bases: List[str]) -> None:
        with _LOCK:
            count = self.db.execute("SELECT COUNT(*) AS c FROM watchlist").fetchone()["c"]
        if count:
            return
        for b in bases:
            self.add_watch(b)

    # ---------- alert config ----------
    def seed_alert_defaults(self) -> None:
        with _LOCK:
            for k, v in DEFAULT_ALERT_CONFIG.items():
                self.db.execute(
                    "INSERT OR IGNORE INTO alert_config(scope, key, value) VALUES ('default', ?, ?)",
                    (k, v),
                )
            self.db.commit()

    def get_alert_config(self) -> Dict[str, Dict[str, float]]:
        with _LOCK:
            rows = self.db.execute("SELECT scope, key, value FROM alert_config").fetchall()
        out: Dict[str, Dict[str, float]] = {}
        for r in rows:
            out.setdefault(r["scope"], {})[r["key"]] = r["value"]
        out.setdefault("default", dict(DEFAULT_ALERT_CONFIG))
        return out

    def resolved_config_for(self, symbol: str) -> Dict[str, float]:
        cfg = self.get_alert_config()
        merged = dict(DEFAULT_ALERT_CONFIG)
        merged.update(cfg.get("default", {}))
        merged.update(cfg.get(symbol, {}))
        return merged

    def set_alert_config(self, scope: str, values: Dict[str, float]) -> None:
        with _LOCK:
            for k, v in values.items():
                if k not in DEFAULT_ALERT_CONFIG:
                    continue
                self.db.execute(
                    "INSERT INTO alert_config(scope, key, value) VALUES (?,?,?) "
                    "ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value",
                    (scope, k, float(v)),
                )
            self.db.commit()

    def clear_alert_config(self, scope: str) -> None:
        if scope == "default":
            return
        with _LOCK:
            self.db.execute("DELETE FROM alert_config WHERE scope = ?", (scope,))
            self.db.commit()

    # ---------- engine params (what-if backtest only) ----------
    def get_engine_params(self) -> Dict[str, Dict[str, float]]:
        with _LOCK:
            rows = self.db.execute("SELECT scope, key, value FROM engine_params").fetchall()
        out: Dict[str, Dict[str, float]] = {}
        for r in rows:
            out.setdefault(r["scope"], {})[r["key"]] = r["value"]
        return out

    def resolved_engine_params(self, symbol: str) -> Dict[str, float]:
        cfg = self.get_engine_params()
        merged = dict(cfg.get("default", {}))
        merged.update(cfg.get(symbol, {}))
        return merged

    def set_engine_params(self, scope: str, values: Dict[str, Any]) -> None:
        with _LOCK:
            for k, v in values.items():
                self.db.execute(
                    "INSERT INTO engine_params(scope, key, value) VALUES (?,?,?) "
                    "ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value",
                    (scope, k, float(1 if v is True else 0 if v is False else v)),
                )
            self.db.commit()

    def clear_engine_params(self, scope: str) -> None:
        with _LOCK:
            self.db.execute("DELETE FROM engine_params WHERE scope = ?", (scope,))
            self.db.commit()

    # ---------- signal events ----------
    def latest_signal(self, symbol: str, kind: Optional[str] = None) -> Optional[dict]:
        q = "SELECT * FROM signal_event WHERE symbol = ?"
        args: List[Any] = [symbol]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY ts DESC LIMIT 1"
        with _LOCK:
            row = self.db.execute(q, args).fetchone()
        return dict(row) if row else None

    def insert_signal_event(self, **kw) -> int:
        cols = [
            "symbol", "ts", "last_seen", "kind", "direction", "setup", "price",
            "entry", "stop_loss", "take_profit_1", "take_profit_2",
            "rsi", "macd", "macd_signal", "adx", "ema_fast", "ema_slow", "source",
        ]
        now = _now()
        kw.setdefault("ts", now)
        kw.setdefault("last_seen", now)
        kw.setdefault("source", "portal")
        vals = [kw.get(c) for c in cols]
        with _LOCK:
            cur = self.db.execute(
                f"INSERT INTO signal_event ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                vals,
            )
            self.db.commit()
            return cur.lastrowid

    def touch_signal(self, event_id: int) -> None:
        with _LOCK:
            self.db.execute(
                "UPDATE signal_event SET last_seen = ? WHERE id = ?", (_now(), event_id)
            )
            self.db.commit()

    def recent_signals(self, limit: int = 100, symbol: Optional[str] = None,
                       kind: Optional[str] = None) -> List[dict]:
        q = "SELECT * FROM signal_event WHERE 1=1"
        args: List[Any] = []
        if symbol:
            q += " AND symbol = ?"
            args.append(symbol)
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with _LOCK:
            rows = self.db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def has_backfill(self) -> bool:
        with _LOCK:
            row = self.db.execute(
                "SELECT 1 FROM signal_event WHERE source = 'backfill' LIMIT 1"
            ).fetchone()
        return row is not None

    def bulk_insert_backfill(self, rows: List[dict]) -> int:
        n = 0
        with _LOCK:
            for r in rows:
                self.db.execute(
                    "INSERT INTO signal_event (symbol, ts, last_seen, kind, direction, setup, price, source) "
                    "VALUES (?,?,?,?,?,?,?, 'backfill')",
                    (r["symbol"], r["ts"], r["ts"], r["kind"], r.get("direction"),
                     r.get("setup"), r.get("price")),
                )
                n += 1
            self.db.commit()
        return n

    def insert_bot_signals(self, rows: List[dict]) -> List[dict]:
        """Insert rows parsed from the bot's output/signals/*.txt that aren't already
        recorded (any source), tagged source='bot'. Returns the newly inserted rows
        (so the poller can broadcast / notify on genuinely new bot activity)."""
        with _LOCK:
            existing = {
                (r["symbol"], r["ts"], r["kind"])
                for r in self.db.execute(
                    "SELECT symbol, ts, kind FROM signal_event"
                ).fetchall()
            }
            fresh = [r for r in rows if (r["symbol"], r["ts"], r["kind"]) not in existing]
            for r in fresh:
                self.db.execute(
                    "INSERT INTO signal_event (symbol, ts, last_seen, kind, direction, setup, price, source) "
                    "VALUES (?,?,?,?,?,?,?, 'bot')",
                    (r["symbol"], r["ts"], r["ts"], r["kind"], r.get("direction"),
                     r.get("setup"), r.get("price")),
                )
            self.db.commit()
        # re-fetch with ids for the caller
        out: List[dict] = []
        for r in fresh:
            row = self.db.execute(
                "SELECT * FROM signal_event WHERE symbol=? AND ts=? AND kind=? AND source='bot' LIMIT 1",
                (r["symbol"], r["ts"], r["kind"]),
            ).fetchone()
            if row:
                out.append(dict(row))
        return out

    # ---------- indicator snapshots ----------
    def insert_snapshot(self, symbol: str, snap: Dict[str, Any]) -> None:
        with _LOCK:
            self.db.execute(
                "INSERT INTO indicator_snapshot "
                "(symbol, ts, price, rsi, macd, macd_signal, macd_hist, adx, ema_fast, ema_slow, atr, vol, vol_avg20, vol_ratio) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    symbol, _now(), snap.get("price"), snap.get("rsi"), snap.get("macd"),
                    snap.get("macd_signal"), snap.get("macd_hist"), snap.get("adx"),
                    snap.get("ema_fast"), snap.get("ema_slow"), snap.get("atr"),
                    snap.get("vol"), snap.get("vol_avg20"), snap.get("vol_ratio"),
                ),
            )
            self.db.commit()

    def snapshots(self, symbol: str, since_hours: int = 48, limit: int = 2000) -> List[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        with _LOCK:
            rows = self.db.execute(
                "SELECT ts, price, rsi, macd, macd_signal, macd_hist, adx, ema_fast, ema_slow, vol, vol_ratio "
                "FROM indicator_snapshot WHERE symbol = ? AND ts >= ? ORDER BY ts ASC LIMIT ?",
                (symbol, cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def prune_snapshots(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with _LOCK:
            cur = self.db.execute("DELETE FROM indicator_snapshot WHERE ts < ?", (cutoff,))
            self.db.commit()
            return cur.rowcount

    # ---------- portal paper positions ----------
    def get_portal_position(self, symbol: str) -> Optional[dict]:
        with _LOCK:
            row = self.db.execute(
                "SELECT * FROM portal_position WHERE symbol = ?", (symbol,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tp1_hit"] = bool(d["tp1_hit"])
        return d

    def all_portal_positions(self) -> Dict[str, dict]:
        with _LOCK:
            rows = self.db.execute("SELECT * FROM portal_position").fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d["tp1_hit"] = bool(d["tp1_hit"])
            out[d["symbol"]] = d
        return out

    def open_portal_position(self, sig: dict) -> None:
        with _LOCK:
            self.db.execute(
                "INSERT OR REPLACE INTO portal_position "
                "(symbol, direction, setup, entry, stop_loss, take_profit_1, take_profit_2, tp1_hit, opened_at) "
                "VALUES (?,?,?,?,?,?,?,0,?)",
                (sig["symbol"], sig["direction"], sig["setup"], sig["entry"],
                 sig["stop_loss"], sig["take_profit_1"], sig["take_profit_2"], _now()),
            )
            self.db.commit()

    def portal_position_breakeven(self, symbol: str) -> None:
        with _LOCK:
            self.db.execute(
                "UPDATE portal_position SET stop_loss = entry, tp1_hit = 1 WHERE symbol = ?",
                (symbol,),
            )
            self.db.commit()

    def close_portal_position(self, symbol: str) -> None:
        with _LOCK:
            self.db.execute("DELETE FROM portal_position WHERE symbol = ?", (symbol,))
            self.db.commit()

    # ================= live trading =================
    def trading_state(self) -> dict:
        """Single row, with lazy UTC-day rollover of the counters."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with _LOCK:
            row = self.db.execute("SELECT * FROM trading_state WHERE id = 1").fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO trading_state (id, day, killed, daily_loss_tripped, orders_today, realized_pnl_today_usdt) "
                    "VALUES (1, ?, 0, 0, 0, 0)", (today,),
                )
                self.db.commit()
                row = self.db.execute("SELECT * FROM trading_state WHERE id = 1").fetchone()
            d = dict(row)
            if d["day"] != today:
                self.db.execute(
                    "UPDATE trading_state SET day = ?, daily_loss_tripped = 0, orders_today = 0, "
                    "realized_pnl_today_usdt = 0 WHERE id = 1", (today,),
                )
                self.db.commit()
                d.update(day=today, daily_loss_tripped=0, orders_today=0, realized_pnl_today_usdt=0.0)
        d["killed"] = bool(d["killed"])
        d["daily_loss_tripped"] = bool(d["daily_loss_tripped"])
        return d

    def set_kill_switch(self, on: bool) -> None:
        self.trading_state()  # ensure row + rollover
        with _LOCK:
            self.db.execute("UPDATE trading_state SET killed = ? WHERE id = 1", (1 if on else 0,))
            self.db.commit()

    def record_order_counter(self, realized_pnl_delta: float, daily_loss_limit: float) -> None:
        self.trading_state()
        with _LOCK:
            self.db.execute(
                "UPDATE trading_state SET orders_today = orders_today + 1, "
                "realized_pnl_today_usdt = realized_pnl_today_usdt + ? WHERE id = 1",
                (realized_pnl_delta,),
            )
            row = self.db.execute(
                "SELECT realized_pnl_today_usdt FROM trading_state WHERE id = 1"
            ).fetchone()
            if row["realized_pnl_today_usdt"] <= -abs(daily_loss_limit):
                self.db.execute("UPDATE trading_state SET daily_loss_tripped = 1 WHERE id = 1")
            self.db.commit()

    def live_positions(self) -> Dict[str, dict]:
        with _LOCK:
            rows = self.db.execute("SELECT * FROM live_position").fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def get_live_position(self, symbol: str) -> Optional[dict]:
        with _LOCK:
            row = self.db.execute(
                "SELECT * FROM live_position WHERE symbol = ?", (symbol,)
            ).fetchone()
        return dict(row) if row else None

    def open_live_position(self, *, symbol: str, mode: str, base_qty: float, entry_px: float,
                           quote_spent: float, stop_loss: Optional[float],
                           take_profit: Optional[float], open_order_id: Optional[str]) -> None:
        with _LOCK:
            self.db.execute(
                "INSERT OR REPLACE INTO live_position "
                "(symbol, mode, base_qty, entry_px, quote_spent, stop_loss, take_profit, open_order_id, opened_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (symbol, mode, base_qty, entry_px, quote_spent, stop_loss, take_profit,
                 open_order_id, _now()),
            )
            self.db.commit()

    def close_live_position(self, *, symbol: str, exit_px: float, quote_out: float,
                            close_order_id: Optional[str]) -> Optional[dict]:
        pos = self.get_live_position(symbol)
        if pos is None:
            return None
        realized = quote_out - pos["quote_spent"]
        with _LOCK:
            self.db.execute(
                "INSERT INTO live_trade (symbol, mode, base_qty, entry_px, exit_px, quote_in, quote_out, "
                "realized_pnl, open_order_id, close_order_id, opened_at, closed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, pos["mode"], pos["base_qty"], pos["entry_px"], exit_px, pos["quote_spent"],
                 quote_out, realized, pos["open_order_id"], close_order_id, pos["opened_at"], _now()),
            )
            self.db.execute("DELETE FROM live_position WHERE symbol = ?", (symbol,))
            self.db.commit()
        return {"realized_pnl": realized, "quote_in": pos["quote_spent"], "quote_out": quote_out}

    def recent_trades(self, limit: int = 50) -> List[dict]:
        with _LOCK:
            rows = self.db.execute(
                "SELECT * FROM live_trade ORDER BY closed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_agent_call(self, **kw) -> int:
        cols = ["ts", "intent", "symbol", "mode", "status", "model", "stop_reason",
                "tool_calls", "text", "error", "guardrail"]
        kw.setdefault("ts", _now())
        with _LOCK:
            cur = self.db.execute(
                f"INSERT INTO agent_call ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [kw.get(c) for c in cols],
            )
            self.db.commit()
            return cur.lastrowid

    def recent_agent_calls(self, limit: int = 30) -> List[dict]:
        with _LOCK:
            rows = self.db.execute(
                "SELECT * FROM agent_call ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
