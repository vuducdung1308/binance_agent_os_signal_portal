"""FastAPI app: REST + WebSocket + static frontend, plus the background poller.

Run:  python -m portal.app       (or ./run.sh)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import bot_bridge as bb
from .backfill import parse_signals_dir
from .bot_health import bot_health
from .chartlab import analyze_chart
from .market import klines_raw, symbol_exists
from .poller import Hub, Poller
from .settings import bot_watchlist, load_portal_settings
from .store import DEFAULT_ALERT_CONFIG, Store
from .trading import TradingService, load_trading_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("portal.app")

settings = load_portal_settings()
store = Store(settings.db_path)
hub = Hub()


def _mark_price(symbol: str):
    row = hub.last_price.get(symbol.upper())
    return row.get("price") if row else None


trading = TradingService(load_trading_config(), store, _mark_price)
poller = Poller(settings, store, hub, trading)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.seed_alert_defaults()
    store.seed_watchlist(bot_watchlist(settings.bot_root))

    if not store.has_backfill():
        rows = parse_signals_dir(bb.BOT_SIGNALS_DIR)
        if rows:
            n = store.bulk_insert_backfill(rows)
            log.info("Backfilled %d historic signals from %s", n, bb.BOT_SIGNALS_DIR)

    pruned = store.prune_snapshots(settings.snapshot_retention_days)
    if pruned:
        log.info("Pruned %d old indicator snapshots", pruned)

    hub.seed_spark(store)
    poller.start()
    log.info(
        "Portal up on http://%s:%d  | bot_root=%s tf=%s watch=%s",
        settings.host, settings.port, settings.bot_root, settings.timeframe,
        ",".join(store.watchlist_symbols()),
    )
    log.info(
        "Telegram: %s",
        f"ON (chat {settings.telegram_chat_id})" if poller.telegram.enabled
        else "OFF (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable)",
    )
    tc = trading.cfg
    log.info(
        "Trading: mode=%s  live_ready=%s  model=%s  limits=%sUSDT/order, %s/day, -%sUSDT/day, %s pos%s",
        tc.mode, tc.live_ready, tc.anthropic_model,
        tc.limits.max_notional_per_order_usdt, tc.limits.max_orders_per_day,
        tc.limits.daily_loss_limit_usdt, tc.limits.max_open_positions,
        "  [KILL_SWITCH env ON]" if tc.env_kill_switch else "",
    )
    if tc.auto_execute:
        log.info(
            "Auto-execute on signals: ENABLED (delay %ss, live %s) — arm it in the Trading panel",
            tc.auto_delay_sec, "allowed" if tc.auto_allow_live else "blocked (set TRADE_AUTO_ALLOW_LIVE=1)",
        )
    try:
        yield
    finally:
        await poller.stop()


app = FastAPI(title="Binance Agent OS Signal Portal", version="1.0", lifespan=lifespan)


# ----------------------------- models -----------------------------
class AddWatch(BaseModel):
    base: str


class ConfigUpdate(BaseModel):
    scope: str  # "default" or a symbol like "BTCUSDT"
    values: Dict[str, float]


class EngineParamsUpdate(BaseModel):
    scope: str
    values: Dict[str, float]


class BacktestRequest(BaseModel):
    days: int = 90
    params: Optional[Dict[str, float]] = None
    compare: bool = True
    use_saved: bool = False


class OrderRequest(BaseModel):
    symbol: str
    intent: str  # "OPEN" | "CLOSE"
    notional_usdt: Optional[float] = None
    confirm: bool = False  # the UI's explicit-confirm gate; must be true


class KillSwitchRequest(BaseModel):
    on: bool


class AutoArmRequest(BaseModel):
    on: bool


class AutoCancelRequest(BaseModel):
    symbol: Optional[str] = None


# ----------------------------- REST -----------------------------
@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "timeframe": settings.timeframe,
            "poll": {"price": settings.price_poll_sec, "signal": settings.signal_poll_sec}}


@app.get("/api/state")
async def get_state() -> dict:
    """Everything the dashboard needs for a cold load: watchlist, last known
    price + indicators per symbol (from the hub cache), config, bot positions."""
    watch = store.get_watchlist()
    bot_positions = bb.read_bot_open_positions()
    portal_positions = store.all_portal_positions()
    coins = []
    for w in watch:
        sym = w["symbol"]
        ind = hub.last_indicators.get(sym)
        coins.append({
            "symbol": sym,
            "base": w["base"],
            "price": hub.last_price.get(sym),
            "indicators": ind,
            "spark": list(hub.spark.get(sym, [])),
            "bot_position": bot_positions.get(sym),
            "portal_position": portal_positions.get(sym),
        })
    return {
        "timeframe": settings.timeframe,
        "engine_defaults": bb.ENGINE_DEFAULTS,
        "poll": {"price": settings.price_poll_sec, "signal": settings.signal_poll_sec},
        "watchlist": watch,
        "coins": coins,
        "config": store.get_alert_config(),
        "config_keys": list(DEFAULT_ALERT_CONFIG.keys()),
        "config_defaults": DEFAULT_ALERT_CONFIG,
    }


@app.get("/api/watchlist")
async def get_watchlist() -> List[dict]:
    return store.get_watchlist()


@app.post("/api/watchlist")
async def add_watchlist(body: AddWatch) -> dict:
    base = body.base.strip().upper()
    if not base:
        raise HTTPException(400, "empty base")
    symbol = base if base.endswith("USDT") else f"{base}USDT"
    if not await asyncio.to_thread(symbol_exists, symbol):
        raise HTTPException(404, f"{symbol} not found on Binance")
    row = store.add_watch(base)
    return {"added": row, "watchlist": store.get_watchlist()}


@app.delete("/api/watchlist/{symbol}")
async def del_watchlist(symbol: str) -> dict:
    store.remove_watch(symbol)
    hub.last_price.pop(symbol.upper(), None)
    hub.last_indicators.pop(symbol.upper(), None)
    return {"removed": symbol.upper(), "watchlist": store.get_watchlist()}


@app.get("/api/config")
async def get_config() -> dict:
    return {
        "keys": list(DEFAULT_ALERT_CONFIG.keys()),
        "defaults": DEFAULT_ALERT_CONFIG,
        "config": store.get_alert_config(),
        "engine_defaults": bb.ENGINE_DEFAULTS,
        "note": "These thresholds only drive the portal's dashboard badges/colours. "
                "The bot's signal engine is unchanged and not configurable here.",
    }


@app.put("/api/config")
async def put_config(body: ConfigUpdate) -> dict:
    bad = [k for k in body.values if k not in DEFAULT_ALERT_CONFIG]
    if bad:
        raise HTTPException(400, f"unknown keys: {bad}")
    store.set_alert_config(body.scope, body.values)
    return {"config": store.get_alert_config()}


@app.delete("/api/config/{scope}")
async def del_config(scope: str) -> dict:
    store.clear_alert_config(scope)
    return {"config": store.get_alert_config()}


@app.get("/api/signals")
async def get_signals(symbol: Optional[str] = None, kind: Optional[str] = None,
                      limit: int = 100) -> List[dict]:
    limit = max(1, min(limit, 500))
    return store.recent_signals(limit=limit, symbol=symbol, kind=kind)


@app.get("/api/klines/{symbol}")
async def get_klines(symbol: str, interval: Optional[str] = None, limit: int = 300) -> dict:
    interval = interval or settings.timeframe
    limit = max(50, min(limit, 1000))
    try:
        raw = await asyncio.to_thread(klines_raw, symbol.upper(), interval, limit)
    except Exception as exc:
        raise HTTPException(502, f"binance klines error: {exc}")
    return {"symbol": symbol.upper(), "interval": interval,
            "candles": [
                {"t": r[0] // 1000, "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
                for r in raw
            ]}


@app.get("/api/chart/{symbol}")
async def get_chart(symbol: str, interval: Optional[str] = None, limit: int = 400) -> dict:
    """Candles + drawing overlays (S/R, trendlines, divergence, liquidity, volume
    profile). Heuristic price-structure aids — not signal inputs."""
    interval = interval or settings.timeframe
    limit = max(120, min(limit, 1000))
    try:
        return await asyncio.to_thread(analyze_chart, symbol.upper(), interval, limit)
    except Exception as exc:
        raise HTTPException(502, f"chart analysis error: {exc}")


@app.get("/api/snapshots/{symbol}")
async def get_snapshots(symbol: str, hours: int = 48) -> dict:
    hours = max(1, min(hours, 24 * 30))
    return {"symbol": symbol.upper(),
            "rows": store.snapshots(symbol.upper(), since_hours=hours)}


@app.get("/api/bot-health")
async def get_bot_health() -> dict:
    return await asyncio.to_thread(bot_health, settings.bot_root)


@app.get("/api/trading/status")
async def trading_status() -> dict:
    st = await asyncio.to_thread(trading.status)
    st["auto_pending"] = poller.auto_pending_list()
    return st


@app.post("/api/trading/order")
async def trading_order(body: OrderRequest) -> dict:
    if not body.confirm:
        raise HTTPException(400, "confirm must be true (the placement is confirmed in the UI first)")
    if body.intent.upper() not in ("OPEN", "CLOSE"):
        raise HTTPException(400, "intent must be OPEN or CLOSE")
    res = await asyncio.to_thread(
        trading.place, body.symbol, body.intent, body.notional_usdt
    )
    if res.get("status") == "error":
        raise HTTPException(502, res.get("error", "order failed"))
    return res


@app.put("/api/trading/kill-switch")
async def trading_kill_switch(body: KillSwitchRequest) -> dict:
    res = await asyncio.to_thread(trading.set_kill_switch, body.on)
    if body.on:  # kill switch on -> drop any queued auto orders
        await poller.cancel_auto()
    res["auto_pending"] = poller.auto_pending_list()
    return res


@app.put("/api/trading/auto")
async def trading_auto_arm(body: AutoArmRequest) -> dict:
    res = await asyncio.to_thread(trading.set_auto_armed, body.on)
    if not body.on:
        await poller.cancel_auto()
    res["auto_pending"] = poller.auto_pending_list()
    return res


@app.post("/api/trading/auto-cancel")
async def trading_auto_cancel(body: AutoCancelRequest) -> dict:
    n = await poller.cancel_auto(body.symbol)
    return {"cancelled": n, "auto_pending": poller.auto_pending_list()}


@app.post("/api/telegram-test")
async def telegram_test() -> dict:
    if not poller.telegram.enabled:
        raise HTTPException(400, "Telegram not configured (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")
    ok = await asyncio.to_thread(
        poller.telegram.send, "✅ Crypto Signal Portal — Telegram test. Signal alerts are on."
    )
    if not ok:
        raise HTTPException(502, "Telegram rejected the message — check the token / chat id")
    return {"sent": True, "chat_id": settings.telegram_chat_id}


@app.get("/api/engine-params")
async def get_engine_params() -> dict:
    return {
        "meta": bb.PARAMS_META,
        "defaults": bb.param_defaults(),
        "saved": store.get_engine_params(),
        "note": "For the what-if backtest only. The live bot and portal signals keep "
                "running the default params — changes here are NOT applied to real trades.",
    }


@app.put("/api/engine-params")
async def put_engine_params(body: EngineParamsUpdate) -> dict:
    clean = bb.clean_param_overrides(body.values)
    store.set_engine_params(body.scope, clean if clean else {})
    if not clean:  # everything equalled default -> nothing to keep
        store.clear_engine_params(body.scope)
    return {"saved": store.get_engine_params()}


@app.delete("/api/engine-params/{scope}")
async def del_engine_params(scope: str) -> dict:
    store.clear_engine_params(scope)
    return {"saved": store.get_engine_params()}


@app.get("/api/backtest/{symbol}")
async def get_backtest(symbol: str, days: int = 90) -> dict:
    """Simple form — engine defaults only."""
    days = max(14, min(days, 365))
    try:
        return await asyncio.to_thread(
            bb.run_backtest, symbol.upper(), settings.timeframe, days, None, False
        )
    except Exception as exc:
        raise HTTPException(502, f"backtest failed: {exc}")


@app.post("/api/backtest/{symbol}")
async def post_backtest(symbol: str, body: BacktestRequest) -> dict:
    """What-if form — runs with tuned params (from the body, or the saved set for this
    symbol when use_saved) and, when compare, also with engine defaults."""
    days = max(14, min(body.days, 365))
    overrides = dict(body.params or {})
    if body.use_saved:
        overrides = {**store.resolved_engine_params(symbol.upper()), **overrides}
    try:
        return await asyncio.to_thread(
            bb.run_backtest, symbol.upper(), settings.timeframe, days, overrides or None, body.compare
        )
    except Exception as exc:
        raise HTTPException(502, f"backtest failed: {exc}")


@app.get("/api/orderflow/{symbol}")
async def get_orderflow(symbol: str) -> dict:
    """Taker buy/sell split + order-book bid/ask imbalance, from the bot's own
    src/order_flow.py (public Binance endpoints). Informational only — the bot
    deliberately does not feed this into its signal engine, and neither do we."""
    if bb.get_order_flow_snapshot is None:
        raise HTTPException(503, "order_flow module unavailable")
    snap = await asyncio.to_thread(bb.get_order_flow_snapshot, symbol.upper())
    if snap is None:
        raise HTTPException(502, "no order-flow data")
    return {
        "symbol": symbol.upper(),
        "taker_buy_pct": snap.taker_buy_pct,
        "taker_sell_pct": snap.taker_sell_pct,
        "bid_ask_ratio": snap.bid_ask_ratio,
    }


# ----------------------------- WebSocket -----------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    await hub.register(ws)
    try:
        await ws.send_json(hub.prime_payload())
        while True:
            # we don't expect client messages; this keeps the socket alive and
            # lets us notice a disconnect promptly
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.unregister(ws)


# ----------------------------- static frontend -----------------------------
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(settings.static_dir / "index.html"))


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)  # empty body — a 204 must not carry content


def main() -> None:
    import uvicorn

    uvicorn.run("portal.app:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
