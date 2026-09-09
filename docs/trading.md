# Live trading via the Binance Agent OS MCP

The portal can place **spot MARKET orders** and track P/L. It is off by default
(`TRADE_MODE=dry-run`). By default every order is a person clicking a button and
confirming it in a dialog; an opt-in [auto-execute mode](#auto-execute-on-signals)
lets the portal place the order itself after a countdown you can cancel.

## How it works

- **dry-run** (default): a fill is simulated at the current reference price minus a taker
  fee. Nothing hits the network. Use it to exercise the whole flow safely.
- **live**: the portal asks **Claude** to place the exact order through the **Binance Agent
  OS MCP** (`https://agent.binance.com/mcp/agentic`), allow-listing only `spot_newOrder`,
  `spot_getAccount`, `spot_tickerPrice`, `spot_getOpenOrders`, `spot_getOrder`. Withdrawals,
  transfers, futures, margin, and unrelated cancels are not reachable. Two executors
  (`TRADE_EXECUTOR`): **`claude-cli`** shells out to the `claude` CLI and reuses an MCP
  server you authenticated in Claude Code (no API key, no raw token); **`anthropic-api`**
  calls the Anthropic Messages API directly with `ANTHROPIC_API_KEY` + a raw OAuth token.

Guardrails (`portal/trading.py`) are the single choke point before any order:

| Limit | Default | Env | Ceiling |
|---|---|---|---|
| USDT notional per order | 10 | `TRADE_MAX_NOTIONAL_USDT` | 100 |
| Orders per UTC day | 5 | `TRADE_MAX_ORDERS_PER_DAY` | 50 |
| Realised loss per UTC day (trips the kill switch) | 10 | `TRADE_DAILY_LOSS_LIMIT_USDT` | 1000 |
| Simultaneously open positions | 1 | `TRADE_MAX_OPEN_POSITIONS` | 3 |

The kill switch (env `KILL_SWITCH` or the manual toggle in the Trading panel) blocks new
**OPEN**s. **CLOSE** is always allowed so a position can be flattened.

## Turning it on

Two executors place the live order. Pick one with `TRADE_EXECUTOR`.

### Executor A — `claude-cli` (recommended: no API key, no raw token)

If you already use the Binance Agent OS MCP inside **Claude Code**, the portal can reuse
that. Claude Code holds the OAuth token (in the OS keychain) and refreshes it; the portal
just shells out to `claude` for each order.

1. Register the MCP server at **user scope** so it works from any directory:
   ```bash
   claude mcp add -s user binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
   ```
2. Authenticate it once: run `claude`, then `/mcp` → `binance-mcp-server` → **Authenticate**
   (log in to Binance, pick the **sub-account**, consent). `claude mcp list` should show it
   **✓ Connected**.
3. In `.env`:
   ```bash
   TRADE_MODE=live
   TRADE_EXECUTOR=claude-cli
   BINANCE_MCP_SERVER_NAME=binance-mcp-server
   CLAUDE_CLI_BIN=/absolute/path/to/claude   # `command -v claude` — needed because a
                                             # LaunchAgent has a minimal PATH
   ```
4. Restart. The portal runs, per order:
   `claude -p "<order>" --output-format stream-json --allowedTools mcp__binance-mcp-server__spot_newOrder,…`
   Only the five spot order/query tools are pre-approved; anything else is denied
   non-interactively. Each order consumes your Claude usage (a few cents).

Then skip to **Verify scopes** below. Steps 1–3 under Executor B are only for the
`anthropic-api` path.

### Executor B — `anthropic-api` (`TRADE_EXECUTOR=anthropic-api`)

### 1. Get an OAuth token for a sub-account

> Do this against a **dedicated Binance sub-account**, never your main account. Fund it
> with a small amount (e.g. ~20–50 USDT) and confirm spot trading is enabled.

The MCP endpoint speaks the MCP OAuth flow. Fastest way to capture a token:

```bash
npx @modelcontextprotocol/inspector
```

1. Transport: **Streamable HTTP** (fall back to **SSE** if needed).
2. URL: `https://agent.binance.com/mcp/agentic`.
3. **Auth Settings → Quick OAuth Flow** → log in to Binance → pick the **sub-account** on
   the consent screen → **Continue** until "Authentication complete".
4. Copy the `access_token`.

(Claude Code / Claude / Codex / VS Code can run the same flow —
`claude mcp add binance --transport http https://agent.binance.com/mcp/agentic` then
`/mcp` → Authenticate — but you still need the raw token for the portal.)

### 2. Configure the portal

In `.env`:

```bash
TRADE_MODE=live
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-5
BINANCE_AGENT_OAUTH_TOKEN=<access_token>
# optional: tighten the limits below the defaults
```

Restart (`./run.sh`). The startup log prints `Trading: mode=live live_ready=True …`, and
the header badge turns **LIVE** (red).

### 3. Place an order

- **Trading panel** (💹 in the header): pick a coin + USDT amount → *Place market BUY* →
  confirm. Open positions show live mark + unrealised P/L; *Close* flattens.
- **Coin modal**: an *Execute BUY* / *Close* button next to the indicators.

Every closed round-trip is stored (`live_trade`), and every agent call is logged
(`agent_call`, shown in the panel's audit table). Realised P/L rolls up per UTC day and
trips the kill switch at the daily-loss limit.

## Auto-execute on signals

Off by default. Two gates: `TRADE_AUTO_ON_SIGNAL=1` in `.env` (a restart), **and** the
**Armed** toggle in the Trading panel. Only then does a signal schedule an order:

| Signal | Scheduled order |
|---|---|
| entry, direction **LONG** | market **BUY** `TRADE_NOTIONAL_USDT` (skipped if already holding it or at the position cap) |
| **exit** (SL/TP/technical reversal) for a symbol you hold | market **CLOSE** of that position |

Between the signal and the order there is a **countdown** (`TRADE_AUTO_DELAY_SEC`, default
30s). It shows in a bar at the top of the page and in the Trading panel, each with a
**Cancel** button, and it pings the browser notification + sound. If you don't cancel, the
order is placed when the timer hits zero — and still has to pass every guardrail above.
Disarming, the kill switch, or closing/opening the position in the meantime all abort it.
Pending countdowns live in memory only — a portal restart drops them (nothing fires).

In `live` mode auto also needs `TRADE_AUTO_ALLOW_LIVE=1` — otherwise a signal in live mode
schedules nothing and you place the order by hand. Auto orders are tagged `source=auto` in
the `agent_call` audit log and get a Telegram line when they fire.

## What the portal never does

- Run the OAuth flow for you, or store your Binance password.
- Auto-execute silently — auto mode is opt-in, armed by hand, and every order has a
  cancellable countdown; with auto off, an order is always a confirmed click.
- Place stop-loss / take-profit / OCO orders — exits are manual or auto `CLOSE`s.
- Touch anything outside the allow-listed spot tools.

Not financial advice. Small-sample, fee-aware only in estimates. DYOR.
