# Binance Agent OS — Signal Portal

A local, single‑user web portal that visualises the technical‑analysis signals produced by
the **Binansquare** trading bot in real time, instead of only receiving them over Telegram.

It is a **read‑mostly companion** to the bot: it reuses the bot's own indicator/signal
engine unchanged, polls Binance's public REST API, pushes updates to the browser over a
WebSocket, and keeps its own history in SQLite. The bot keeps running exactly as before.

```
┌──────────────────────────┐        imports (read-only)        ┌─────────────────────────┐
│  Binansquare bot          │  ───────────────────────────────▶ │  Signal Portal          │
│  (separate repo/folder)   │   src/indicators, src/signal_…    │  FastAPI + WS + SQLite   │
│  runs on its own          │   data/open_positions.json        │  vanilla-JS frontend    │
│  LaunchAgent schedulers   │   output/signals/*.txt            │  one process, port 8777 │
└──────────────────────────┘                                    └─────────────────────────┘
```

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [The bot dependency (`BOT_ROOT` contract)](#the-bot-dependency-bot_root-contract)
- [Quick start](#quick-start)
- [Step-by-step replication](#step-by-step-replication)
- [How it works](#how-it-works)
- [Configuration reference](#configuration-reference)
- [HTTP / WebSocket API](#http--websocket-api)
- [Data & persistence](#data--persistence)
- [Changes made to the bot](#changes-made-to-the-bot)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Limitations & non-goals](#limitations--non-goals)

---

## What it does

| Area | Detail |
|---|---|
| **Live dashboard** | Per‑coin card: price + 24h %, price sparkline, RSI / ADX (with bars), MACD histogram, `vol / avg20`, EMA50 / EMA200, trend badge, alert badges, latest signal, and any open bot / portal position. |
| **Watchlist** | Add / remove coins from the UI; validated against Binance; persisted in SQLite (seeded once from the bot's `SIGNAL_COINS`). Survives restarts. |
| **Alert thresholds** | Per‑coin or default overrides for RSI / ADX / MACD‑hist / volume‑ratio badges. Applied on the next scan, no restart. **Display only** — they do not change the bot engine. |
| **"Why no signal yet"** | A read‑only mirror of the engine's entry gates: for each setup, which conditions currently pass / fail and by how much. |
| **Order flow** | Taker buy/sell split and order‑book bid/ask imbalance (from the bot's `src/order_flow.py`). Reference only. |
| **Backtest** | Runs the bot's real `backtest.py` over history. Edit indicator settings (ADX gate, RSI bands, ATR multiples, look‑backs, EMA/periods, SHORT toggle) and compare **Default vs Yours** side by side. Save a tuned set per coin. What‑if only — never applied to live trades. |
| **Signal history** | Every entry/exit kept in SQLite indefinitely. Backfilled from the bot's `output/signals/*.txt` on first run, then kept in sync every scan. |
| **Bot health** | Header strip showing each bot scheduler's state, from `launchctl` (falls back to log‑file mtime). |
| **Notifications** | Optional desktop notification + sound on a new signal, and optional Telegram push (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) for every signal the portal detects. |
| **Live trading** (opt-in) | Place spot MARKET orders through the **Binance Agent OS MCP** (Claude drives an OAuth-scoped, spot-only toolset). `dry-run` by default — simulated, no network. Manual + confirm only, no auto-execution. Guardrails (per-order cap, orders/day, daily-loss kill switch, one position). Tracks open positions with live unrealised P/L, closed-trade realised P/L, and an agent audit log. See [docs/trading.md](docs/trading.md). |

---

## Architecture

A deliberately light design — **one process**:

- **`portal/app.py`** — FastAPI app. Serves the static frontend, the REST API, and the
  `/ws` WebSocket. Starts the background poller on startup (via `lifespan`).
- **`portal/poller.py`** — two independent `asyncio` loops and the WebSocket fan‑out hub:
  - **price loop** (`~5 s`): one batched `GET /api/v3/ticker/24hr` for the whole
    watchlist → broadcast price/volume/%‑change.
  - **signal loop** (`~30 s`): per symbol → fetch closed candles → recompute every
    indicator → run the bot's `compute_entry_signal` / `compute_exit_event` → update the
    portal's own paper positions → persist a snapshot → broadcast indicators + any new
    signal. Also re‑ingests the bot's `output/signals/*.txt` each pass.
- **`portal/bot_bridge.py`** — the **only** module that reaches into the bot. Puts
  `BOT_ROOT` on `sys.path` and re‑exports the bot's functions; adds `analyze()`
  (snapshot + engine‑condition diagnostics in one pass) and a parameterised
  `run_backtest()`.
- **`portal/store.py`** — SQLite: watchlist, alert thresholds, engine‑param overrides,
  signal history, indicator snapshots, portal paper positions, and (if trading is on)
  live positions / trades / agent audit.
- **`portal/market.py`** — the few Binance calls the bot doesn't expose in the needed
  shape (batched 24h ticker; raw klines *including* the forming candle, for the chart;
  symbol validation).
- **`portal/bot_health.py`** — `launchctl` + log‑mtime probe of the bot's schedulers.
- **`portal/notify.py`** — best‑effort Telegram push for detected signals.
- **`portal/trading.py`** — optional spot execution: guardrails, a dry‑run simulator, and
  a live executor that drives the Binance Agent OS MCP through Claude. Off by default.
- **`static/`** — `index.html` + `app.js` + `style.css`. No build step. The candlestick
  chart uses `lightweight-charts` from a CDN; everything else is hand‑rolled.

**Why REST polling, not a Binance WebSocket stream?** The bot computes indicators on
*closed* candles only (`src/price_data.py._drop_unclosed`), so indicator values change at
most once per candle. A 5–30 s poll is well inside Binance's rate limits for ~20 symbols
and keeps the portal a faithful mirror of what the bot acts on. Live price gets its own
fast loop for the real‑time feel.

---

## Prerequisites

- **Python 3.9+** (developed and tested on 3.9.6; the code uses
  `from __future__ import annotations` so 3.9 is fine).
- **macOS or Linux** for `run.sh` (`lsof`). Windows: run `python -m portal.app` directly.
- **`launchctl`** is macOS‑only and used purely for the bot‑health strip; on Linux/Windows
  that strip degrades gracefully to log‑mtime.
- Outbound HTTPS to `api.binance.com` and (for the chart) `cdn.jsdelivr.net`.
- No Binance API key is required — only public market‑data endpoints are used.

---

## The bot dependency (`BOT_ROOT` contract)

**A copy of the bot's engine ships in [`bot/`](bot/)**, so `git clone` + `./run.sh` works
with nothing else installed. `BOT_ROOT` defaults to `./bot`.

Point `BOT_ROOT` at your **own full Binansquare checkout** to also see its live paper
positions (`data/open_positions.json`) and pick up new signals it writes
(`output/signals/*.txt`). The bundled copy has neither, so those two panels stay empty —
everything else (indicators, engine diagnostics, order flow, backtest) works fully.

The bundled `bot/` contains only the pure‑logic modules the portal imports — no Telegram
tokens, no API keys, none of the bot's other pipelines. It can drift from the upstream
bot; treat it as a pinned snapshot.

Whatever you point `BOT_ROOT` at must expose this surface:

**Python modules** (`<BOT_ROOT>` must be importable; it needs a `src/` package):

| Import | Used for |
|---|---|
| `src.indicators` → `ema, rsi, macd, atr, adx` | recomputing indicators for display |
| `src.price_data` → `Candle`, `fetch_klines(symbol, interval, limit)` | closed OHLCV candles, oldest‑first |
| `src.price_watch` → `fetch_current_price(symbol)` | (reserved) |
| `src.signal_engine` → `compute_entry_signal`, `compute_exit_event`, `SignalParams`, `DEFAULT_PARAMS`, `min_candles_required`, `MIN_CANDLES_REQUIRED`, and the module constants | entry/exit detection + diagnostics + backtest params |
| `src.order_flow` → `get_order_flow_snapshot(symbol)` | *optional* — order‑flow panel |
| `backtest` (root‑level) → `run_backtest(symbol, timeframe, days, params=DEFAULT_PARAMS)` | *optional* — the Backtest tab |

**Files** (all read‑only; the portal never writes under `BOT_ROOT`):

| Path | Used for |
|---|---|
| `<BOT_ROOT>/.env` | `SIGNAL_COINS` (seeds the watchlist once), `SIGNAL_TIMEFRAME` |
| `<BOT_ROOT>/data/open_positions.json` | the bot's live paper positions, shown on cards |
| `<BOT_ROOT>/output/signals/*.txt` | signal history — backfilled, then re‑scanned each pass. Filenames must match `SYMBOL_(entry\|exit)_YYYYMMDD_HHMMSS.txt` (timestamp is treated as **system local time**) |
| `<BOT_ROOT>/logs/*.log` | bot‑health fallback |

If `src.order_flow` or `backtest` is missing, those two features disable themselves and
the rest of the portal works.

---

## Quick start

```bash
git clone https://github.com/vuducdung1308/binance_agent_os_signal_portal.git
cd binance_agent_os_signal_portal
./run.sh                     # creates .venv, installs deps, starts on :8777
```

Open <http://127.0.0.1:8777>. The bot's engine is bundled in `bot/`, so there is nothing
else to install or configure.

---

## Step-by-step replication

### 1. Get the code

```bash
git clone https://github.com/vuducdung1308/binance_agent_os_signal_portal.git
# or, with an SSH key configured:
# git clone git@github.com:vuducdung1308/binance_agent_os_signal_portal.git
cd binance_agent_os_signal_portal
```

Starting from a bare copy of these files instead of a clone? Initialise the repo yourself:

```bash
git init
git add .
git commit -m "chore: import Binance Agent OS Signal Portal"
```

`.gitignore` already excludes `.venv/`, `data/portal.db*`, `.env`, `__pycache__/`,
`.DS_Store`, and the bundled bot's runtime output, so the working tree is safe to commit
as‑is (no secrets, no build output).

### 2. (Optional) Point it at your own bot

Skip this to use the bundled engine in `bot/`.

To visualise your **own** Binansquare checkout instead — and see its live paper positions
and new signals — set `BOT_ROOT` to its absolute path (in `.env` or the environment). It
must satisfy the [contract above](#the-bot-dependency-bot_root-contract). It does **not**
need the bot's virtualenv or API keys.

### 3. Create the environment

`run.sh` does this for you, but to do it by hand:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` is just `fastapi`, `uvicorn[standard]`, `requests`, `python-dotenv` —
the bundled bot modules need nothing beyond `requests` + `python-dotenv`.

### 4. (Optional) Configure

Every setting has a working default. To change any of them:

```bash
cp .env.example .env
$EDITOR .env
```

See [Configuration reference](#configuration-reference). Environment variables also work
and take precedence over `.env`.

### 5. Run

```bash
./run.sh
# or, without the launcher:
python -m portal.app
```

`run.sh` also kills any stale process still holding `PORTAL_PORT` before starting (a
common foot‑gun — see [Troubleshooting](#troubleshooting)).

### 6. First run — what happens

On the first startup the portal will:

1. Create `data/portal.db` and its schema.
2. Seed `alert_config` with the default display thresholds.
3. Seed the `watchlist` from `<BOT_ROOT>/.env` `SIGNAL_COINS` (bundled bot →
   `BTC,ETH,BNB,ADA,SOL,LTC`).
4. Backfill `signal_event` from every parseable `<BOT_ROOT>/output/signals/*.txt`
   (none with the bundled bot — that folder is empty).
5. Prune `indicator_snapshot` rows older than the retention window (none yet).
6. Start the two poll loops.

You will see a log line like:

```
INFO portal.app Portal up on http://127.0.0.1:8777 | bot_root=… tf=1h watch=BTCUSDT,ETHUSDT,…
```

### 7. Verify

```bash
curl -s localhost:8777/api/health
curl -s localhost:8777/api/state | python -m json.tool | head -40
curl -s localhost:8777/api/bot-health | python -m json.tool
```

In the browser, the connection dot should be **live**, cards should fill in within ~30 s
(one signal‑loop pass), and the header should show `bot: 🟢…`.

---

## How it works

- **Two cadences.** Price (`PORTAL_PRICE_POLL_SEC`) is a single batched request for the
  whole watchlist. Indicators/signals (`PORTAL_SIGNAL_POLL_SEC`) are per‑symbol: fetch
  `PORTAL_KLINES_LIMIT` closed candles → `bot_bridge.analyze()` (indicator snapshot +
  engine‑condition diagnostics) → `compute_entry_signal` / `compute_exit_event`.
- **Portal paper positions.** When the engine returns an entry and the portal has no open
  position for that symbol, it opens one in `portal_position` (SQLite) and tracks its
  exit with `compute_exit_event` — a self‑contained mirror, independent of the bot's
  `data/open_positions.json`, which the portal only *reads*.
- **Signal history stays in sync.** Beyond the one‑time backfill, every signal‑loop pass
  re‑scans `output/signals/*.txt` and ingests anything new the bot wrote (e.g. a live
  `SL_HIT` the bot's own fast watcher fired), tagged `source='bot'`.
- **Alert thresholds vs engine params.** `alert_config` only colours the dashboard.
  `engine_params` feeds the **what‑if backtest** and is never applied to the live engine
  or to the portal's own paper positions.
- **WebSocket.** On connect, the client is primed with the last known price + indicator
  payload per symbol; thereafter it receives `price_batch`, `indicators`, and `signal`
  messages. Reconnects automatically with backoff.
- **Telegram.** If `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` are set, every new `signal`
  event is also pushed to that chat (best‑effort, never blocks the loop). The first
  signal‑loop pass only seeds state — it does not notify the backlog — so you only get
  messages for signals that fire *after* startup. `POST /api/telegram-test` verifies the
  wiring.
- **Trading** (`portal/trading.py`). `dry-run` by default. An order is proposed by a
  click, checked against the guardrails, then either simulated (dry-run) or handed to
  Claude via the Anthropic Messages API with an `mcp_servers` connector to the Binance
  Agent OS MCP (`anthropic-beta: mcp-client-2025-11-20`, default-deny `mcp_toolset`
  allow-listing spot order/query tools). Fills update `live_position`; a close writes a
  `live_trade` row and rolls realised P/L into the day counter. Every agent call is
  recorded in `agent_call`. Full walkthrough: [docs/trading.md](docs/trading.md).

---

## Configuration reference

All optional. Set via `.env` in the project root or as environment variables (env wins).

| Variable | Default | Meaning |
|---|---|---|
| `BOT_ROOT` | `./bot` | Path to the bot's engine. Bundled copy by default; set to your own Binansquare checkout to also show its live positions/signals. |
| `PORTAL_HOST` | `127.0.0.1` | Bind address. |
| `PORTAL_PORT` | `8777` | Bind port. |
| `PORTAL_PRICE_POLL_SEC` | `5` | Live price/volume refresh interval. |
| `PORTAL_SIGNAL_POLL_SEC` | `30` | Indicator/signal recompute interval. |
| `PORTAL_SNAPSHOT_INTERVAL_SEC` | `60` | How often a snapshot row is written to SQLite. |
| `PORTAL_SNAPSHOT_RETENTION_DAYS` | `30` | Snapshot rows older than this are pruned at startup. |
| `PORTAL_TIMEFRAME` | *(inherits `SIGNAL_TIMEFRAME` from the bot, else `1h`)* | Candle timeframe. |
| `PORTAL_KLINES_LIMIT` | `300` | Candles fetched per symbol per scan (must exceed slow EMA + warm‑up). |
| `PORTAL_DB` | `./data/portal.db` | SQLite file path. |
| `TELEGRAM_BOT_TOKEN` | *(none)* | Set with `TELEGRAM_CHAT_ID` to push a message for every signal the portal detects. Separate from the bot's own `TELEGRAM_*`. |
| `TELEGRAM_CHAT_ID` | *(none)* | Target chat for the push. |
| `PORTAL_TELEGRAM_INCLUDE_BOT` | `false` | Also push signals ingested from the bot's `output/signals/*.txt`. Leave off when `BOT_ROOT` is a real bot that already sends its own. |
| `TRADE_MODE` | `dry-run` | `live` enables real spot orders via the MCP. |
| `KILL_SWITCH` | `0` | Any truthy value blocks new OPENs (CLOSE stays allowed). |
| `TRADE_NOTIONAL_USDT` | `10` | USDT per manual OPEN. |
| `TRADE_MAX_NOTIONAL_USDT` / `TRADE_MAX_ORDERS_PER_DAY` / `TRADE_DAILY_LOSS_LIMIT_USDT` / `TRADE_MAX_OPEN_POSITIONS` | `10 / 5 / 10 / 1` | Guardrails. Env can tighten or modestly raise, up to a hard ceiling (`100 / 50 / 1000 / 3`). |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | *(none)* / `claude-sonnet-5` | Execution agent (live mode only). |
| `BINANCE_AGENT_MCP_URL` / `BINANCE_AGENT_OAUTH_TOKEN` | `…/mcp/agentic` / *(none)* | The MCP connector + a sub-account OAuth token (live mode only — see [docs/trading.md](docs/trading.md)). |
| `PYTHON` | `python3` | Interpreter `run.sh` uses to create `.venv`. |

---

## HTTP / WebSocket API

Base URL `http://127.0.0.1:8777`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness + timeframe + poll intervals. |
| `GET` | `/api/state` | Full cold‑load snapshot: watchlist, per‑coin price/indicators/positions/spark, config. |
| `GET` | `/api/watchlist` | Current watchlist. |
| `POST` | `/api/watchlist` | `{ "base": "DOGE" }` → validate on Binance and add. |
| `DELETE` | `/api/watchlist/{symbol}` | Remove (e.g. `DOGEUSDT`). |
| `GET` | `/api/config` | Alert thresholds: defaults + per‑scope overrides + engine defaults + note. |
| `PUT` | `/api/config` | `{ "scope": "BTCUSDT"\|"default", "values": { "rsi_overbought": 68 } }`. |
| `DELETE` | `/api/config/{scope}` | Clear a scope's overrides. |
| `GET` | `/api/engine-params` | Tunable `SignalParams` fields, defaults, saved overrides, field metadata. |
| `PUT` | `/api/engine-params` | `{ "scope": "...", "values": { ... } }` — saved for the what‑if backtest only. |
| `DELETE` | `/api/engine-params/{scope}` | Clear saved engine params for a scope. |
| `GET` | `/api/backtest/{symbol}?days=90` | Backtest with engine defaults. |
| `POST` | `/api/backtest/{symbol}` | `{ "days": 90, "params": {…}, "compare": true, "use_saved": false }` → tuned + default side by side. |
| `GET` | `/api/signals?symbol=&kind=&limit=` | Recorded signal events (most recent first). |
| `GET` | `/api/klines/{symbol}?interval=&limit=` | Raw candles **including** the forming one (chart only). |
| `GET` | `/api/snapshots/{symbol}?hours=48` | Stored indicator time series. |
| `GET` | `/api/orderflow/{symbol}` | Taker buy/sell split + bid/ask ratio. |
| `GET` | `/api/bot-health` | Per‑scheduler status from `launchctl` / log mtime. |
| `POST` | `/api/telegram-test` | Send a test message to the configured chat. |
| `GET` | `/api/trading/status` | Mode, guardrails, kill switch, open positions + live P/L, closed trades, agent audit. |
| `POST` | `/api/trading/order` | `{ "symbol", "intent": "OPEN"\|"CLOSE", "notional_usdt"?, "confirm": true }` — `confirm` required. |
| `PUT` | `/api/trading/kill-switch` | `{ "on": bool }` — the manual kill switch. |
| `WS` | `/ws` | `prime`, then `price_batch` / `indicators` / `signal`. |
| `GET` | `/` , `/static/*` | Frontend. |

---

## Data & persistence

Everything lives in **`data/portal.db`** (SQLite). Delete it to start clean; it will be
rebuilt on the next launch.

| Table | Contents |
|---|---|
| `watchlist` | tracked symbols + display order |
| `alert_config` | display thresholds (`scope` = `default` or a symbol) |
| `engine_params` | saved `SignalParams` overrides for the what‑if backtest |
| `signal_event` | entry/exit history. `source` ∈ `portal` (portal‑detected), `bot` (ingested from the bot's `.txt`), `backfill` (first‑run import) |
| `indicator_snapshot` | indicator time series for sparklines/history; auto‑pruned |
| `portal_position` | the portal's own paper positions (one per symbol) |
| `trading_state` | day‑scoped order/P‑L counters + the manual kill switch |
| `live_position` | open spot positions the portal placed (dry‑run or live) |
| `live_trade` | closed round‑trips with realised P/L |
| `agent_call` | every execution‑agent call, for audit |

`.gitignore` already excludes `.venv/`, `data/portal.db*`, `.env`, `__pycache__/`.

---

## Changes made to the bot

The bundled `bot/` already contains the parameterised engine. If you point `BOT_ROOT` at
your own Binansquare checkout, it needs the same two changes — made in a strictly
backward‑compatible way so the live bot is byte‑for‑byte unaffected:

| File | Change |
|---|---|
| `src/signal_engine.py` | Added `@dataclass(frozen=True) SignalParams` (19 fields, each defaulting to the existing module constant) + `DEFAULT_PARAMS` + `min_candles_required(params)`. `compute_entry_signal`, `compute_exit_event`, `_Indicators`, `_risk_targets` now take `params: SignalParams = DEFAULT_PARAMS`; bodies use `params.<field>` instead of the bare constant. The old module constants remain. |
| `backtest.py` | `run_backtest(...)` takes `params: SignalParams = DEFAULT_PARAMS` and threads it through the two compute calls. |

**Every existing caller** (`main_signals.py`, the schedulers, `python backtest.py BTC
--days N`) passes nothing → identical behaviour. Verified: `python backtest.py BTC --days
45` produces **byte‑identical** output before and after. In the upstream bot the pre‑change
files are kept as `src/signal_engine.py.pre-params.bak` and `backtest.py.pre-params.bak`.

The **live bot always uses `DEFAULT_PARAMS`.** Tuned params only reach the what‑if
backtest — never the live engine or the portal's own paper positions.

---

## Project layout

```
.
├── portal/
│   ├── app.py          # FastAPI app: REST + /ws + static + lifespan(poller)
│   ├── poller.py       # price loop, signal loop, WebSocket hub, bot-signal ingest
│   ├── bot_bridge.py   # the ONLY link to BOT_ROOT: re-exports + analyze() + run_backtest()
│   ├── store.py        # SQLite schema + all queries
│   ├── market.py       # batched 24h ticker, raw klines, symbol validation
│   ├── backfill.py     # parse output/signals/*.txt → signal rows
│   ├── bot_health.py   # launchctl / log-mtime probe
│   ├── notify.py       # Telegram push for detected signals
│   ├── trading.py      # guardrails + dry-run + live executor (Claude + MCP) + P/L
│   └── settings.py     # env-driven PortalSettings
├── docs/trading.md     # how to enable + authorize live trading
├── static/
│   ├── index.html      # single page, no framework
│   ├── app.js          # dashboard, WS client, modal, backtest form, trading panel
│   └── style.css
├── bot/                # bundled snapshot of the bot's engine (BOT_ROOT default)
│   ├── src/            # indicators, price_data, price_watch, signal_engine, order_flow
│   ├── backtest.py
│   ├── .env            # SIGNAL_COINS + SIGNAL_TIMEFRAME only (no secrets)
│   ├── data/           # empty — a real checkout has open_positions.json here
│   └── output/signals/ # empty — a real checkout has the bot's *.txt drafts here
├── data/               # portal.db is created here at runtime
├── requirements.txt
├── run.sh              # venv + install + free-the-port + launch
├── .env.example
└── README.md
```

---

## Troubleshooting

**The page renders but data is stale / new endpoints 404 after an edit.**
An old portal process is still bound to the port and keeps serving previous backend code
(static files are read fresh from disk, so the UI *looks* updated). `run.sh` now kills the
stale listener automatically; to do it by hand:

```bash
lsof -ti tcp:8777 | xargs kill -9
```

**`sqlite3.OperationalError: attempt to write a readonly database`.**
Almost always transient churn — e.g. deleting `data/portal.db` while a server still holds
it open. Stop all portal processes, remove `data/portal.db*`, restart. Snapshot writes are
wrapped so a hiccup can't stall the live dashboard.

**`bot: 🟡` for `watch` / `news` / `hotmovers`.**
Those schedulers barely write to their logs between runs, so log‑mtime alone looks stale.
If `launchctl` is available the strip uses that instead. `🟡` = "can't confirm", not
"down".

**Chart area shows "Chart library failed to load".**
`cdn.jsdelivr.net` is unreachable. The rest of the portal is unaffected; candle data is
still available via `/api/klines/{symbol}`.

**`ModuleNotFoundError: No module named 'src'`.**
A custom `BOT_ROOT` is wrong or has no importable `src/` package. Print the resolved
path: `python -c "from portal.settings import load_portal_settings as f; print(f().bot_root)"`.
Unset `BOT_ROOT` to fall back to the bundled `bot/`.

**Everything imports but no signals ever appear.**
Expected if none of the watchlist coins currently meet the engine's entry conditions.
Open a coin → "Engine conditions" shows exactly which gates are unmet.

---

## Limitations & non-goals

- **Local, single user, no auth.** Bind stays on `127.0.0.1`. Do not expose it — with
  live trading configured, the process holds an OAuth token that can place spot orders.
- **Trading is opt-in and manual.** Default `dry-run` places no orders. Even in `live`
  mode there is no auto-execution: every order is a confirmed click, capped by
  guardrails, on a Binance **sub-account** you authorize yourself. No withdrawals, no
  transfers, no SL/TP orders. **Not financial advice.**
- **Portal paper positions are close‑candle only.** Unlike the bot's dedicated fast
  watcher, the portal checks its *own* paper exits on the 30 s closed‑candle pass, so an
  intrabar SL/TP touch can lag by up to one candle. Real trades are covered by the bot.
- **`lightweight-charts` from CDN.** The one external asset. Offline, the chart degrades;
  nothing else does.
- **Backtests are small‑sample and fee‑free** — directional evidence, not proof. The UI
  says so on every result.
