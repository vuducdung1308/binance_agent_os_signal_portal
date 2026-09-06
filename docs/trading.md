# Live trading via the Binance Agent OS MCP

The portal can place **spot MARKET orders** and track P/L. It is off by default
(`TRADE_MODE=dry-run`) and there is **no auto-execution** — every order is a person
clicking a button and confirming it in a dialog.

## How it works

- **dry-run** (default): a fill is simulated at the current reference price minus a taker
  fee. Nothing hits the network. Use it to exercise the whole flow safely.
- **live**: the portal asks **Claude** (Anthropic Messages API) to place the exact order
  through the **Binance Agent OS MCP** connector
  (`https://agent.binance.com/mcp/agentic`), with a default-deny toolset that allow-lists
  only `spot_newOrder`, `spot_getAccount`, `spot_tickerPrice`, `spot_getOpenOrders`,
  `spot_getOrder`. Withdrawals, transfers, futures, margin, and unrelated cancels are not
  reachable.

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

## What the portal never does

- Run the OAuth flow for you, or store your Binance password.
- Auto-execute — an order is always a confirmed click.
- Place stop-loss / take-profit / OCO orders — exits are manual `CLOSE`s.
- Touch anything outside the allow-listed spot tools.

Not financial advice. Small-sample, fee-aware only in estimates. DYOR.
