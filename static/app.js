"use strict";

const $ = (s) => document.querySelector(s);
const fmtNum = (v, d = 2) =>
  v == null || isNaN(v) ? "–" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const priceDec = (v) => (v >= 100 ? 2 : v >= 1 ? 4 : 6);

const SPARK_CAP = 240;

const state = {
  tf: "1h",
  coins: new Map(), // symbol -> { symbol, base, price, indicators, spark:[], bot_position, portal_position }
  cfg: null,
  cfgKeys: [],
  cfgDefaults: {},
  engineDefaults: {},
  engineParams: null, // { meta, defaults, saved } from /api/engine-params
};

const view = {
  sort: localStorage.getItem("gridSort") || "default",
  fSignal: localStorage.getItem("fSignal") === "1",
  fAdx: localStorage.getItem("fAdx") === "1",
  fPos: localStorage.getItem("fPos") === "1",
};

const alertPrefs = {
  enabled: localStorage.getItem("notifyEnabled") === "1",
};
let audioCtx = null;
let feedLoaded = false;

// ---------------- init ----------------
async function boot() {
  const s = await fetch("/api/state").then((r) => r.json());
  state.tf = s.timeframe;
  state.cfg = s.config;
  state.cfgKeys = s.config_keys;
  state.cfgDefaults = s.config_defaults;
  state.engineDefaults = s.engine_defaults;
  $("#tf").textContent = s.timeframe;
  $("#pp").textContent = s.poll.price;
  $("#sp").textContent = s.poll.signal;

  for (const c of s.coins) {
    state.coins.set(c.symbol, {
      symbol: c.symbol,
      base: c.base,
      price: c.price,
      indicators: c.indicators,
      spark: c.spark || [],
      bot_position: c.bot_position,
      portal_position: c.portal_position,
    });
  }
  initControls();
  renderBell();
  renderGrid();
  loadFeed();
  connectWS();
  refreshBotHealth();
  setInterval(refreshBotHealth, 60000);
  refreshTradeBadge();
  setInterval(refreshTradeBadge, 15000);
}

// ---------------- bot health strip ----------------
function _ageStr(s) {
  if (s == null) return "–";
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  return Math.round(s / 3600) + "h";
}
async function refreshBotHealth() {
  try {
    const h = await fetch("/api/bot-health").then((r) => r.json());
    const dot = { ok: "🟢", warn: "🟡", idle: "🟡", down: "🔴", crashed: "🔴", unknown: "⚪" };
    const parts = Object.entries(h.agents).map(([name, a]) => `${dot[a.status] || "⚪"}${name}`);
    const el = $("#botHealth");
    el.textContent = "bot: " + parts.join(" ");
    el.title =
      "Bot schedulers (source: launchctl, falls back to log mtime)\n" +
      "🟢 running OK · 🟡 running, but the last run ended badly / can't confirm · 🔴 not running · ⚪ unknown\n\n" +
      Object.entries(h.agents)
        .map(([n, a]) => {
          const lc = a.launchctl
            ? `pid ${a.launchctl.pid ?? "-"}, last exit ${a.launchctl.last_exit}`
            : "not in launchctl";
          return `${n}: ${a.status} (${a.source}) — ${lc} — log ${_ageStr(a.age_sec)} ago`;
        })
        .join("\n");
  } catch (e) {
    $("#botHealth").textContent = "bot: ?";
  }
}

// ---------------- websocket ----------------
let ws, wsRetry = 0;
function connectWS() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => {
    wsRetry = 0;
    $("#wsdot").className = "dot live";
    $("#wsstat").textContent = "live";
  };
  ws.onclose = () => {
    $("#wsdot").className = "dot dead";
    $("#wsstat").textContent = "reconnecting…";
    wsRetry = Math.min(wsRetry + 1, 6);
    setTimeout(connectWS, wsRetry * 1000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => handleMsg(JSON.parse(ev.data));
}

function handleMsg(m) {
  if (m.type === "prime") {
    (m.prices || []).forEach(applyPrice);
    (m.indicators || []).forEach(applyIndicators);
  } else if (m.type === "price_batch") {
    m.rows.forEach(applyPrice);
    $("#upd").textContent = new Date().toLocaleTimeString();
  } else if (m.type === "price") {
    applyPrice(m);
  } else if (m.type === "indicators") {
    applyIndicators(m);
    $("#upd").textContent = new Date().toLocaleTimeString();
  } else if (m.type === "signal") {
    prependFeed(m.event, true);
    fireAlert(m.event);
  }
}

function applyPrice(row) {
  const c = state.coins.get(row.symbol);
  if (!c) return;
  const prev = c.price && c.price.price;
  c.price = row;
  updateCardPrice(row.symbol, prev);
}

function applyIndicators(payload) {
  const c = state.coins.get(payload.symbol);
  if (!c) return;
  c.indicators = payload;
  c.bot_position = payload.bot_position;
  c.portal_position = payload.portal_position;
  if (payload.spark_point) {
    c.spark = c.spark || [];
    c.spark.push(payload.spark_point);
    if (c.spark.length > SPARK_CAP) c.spark.splice(0, c.spark.length - SPARK_CAP);
  }
  renderCard(payload.symbol);
  scheduleRelayout();
  if (modalSym && payload.symbol === modalSym) renderDiag(c);
}

// ---------------- grid ----------------
function renderGrid() {
  const g = $("#grid");
  g.innerHTML = "";
  for (const sym of state.coins.keys()) {
    const el = document.createElement("div");
    el.className = "card";
    el.id = "card-" + sym;
    el.onclick = (e) => {
      if (e.target.classList.contains("rm")) return;
      openChart(sym);
    };
    g.appendChild(el);
    renderCard(sym);
  }
  layoutGrid();
}

function nearSignal(c) {
  const d = c.indicators && c.indicators.diagnostics;
  return !!(d && d.closest_missing && d.closest_missing.length > 0 && d.closest_missing.length <= 1);
}
function hasSignal(c) {
  return !!(
    (c.indicators && c.indicators.engine_signal) ||
    c.portal_position ||
    (c.indicators && c.indicators.diagnostics && c.indicators.diagnostics.any_match)
  );
}

function sigLine(c) {
  const ind = c.indicators || {};
  const entry = ind.latest_entry;
  const exit = ind.latest_exit;
  const eng = ind.engine_signal;
  const pos = c.portal_position;
  const diag = ind.diagnostics;

  let latest = null;
  if (entry && exit) latest = new Date(exit.ts) > new Date(entry.ts) ? exit : entry;
  else latest = entry || exit;

  const posLine = pos
    ? `<div class="pos-open">▸ Portal holding ${pos.direction} from ${fmtNum(pos.entry, priceDec(pos.entry))}${pos.tp1_hit ? " · TP1 done, SL→BE" : ""}</div>`
    : "";
  const botLine = c.bot_position
    ? `<div class="pos-open">▸ Bot holding ${c.bot_position.direction} from ${fmtNum(c.bot_position.entry, priceDec(c.bot_position.entry))}</div>`
    : "";

  const holding = pos || c.bot_position;
  const ageH = latest ? (Date.now() - new Date(latest.ts).getTime()) / 3.6e6 : Infinity;
  const stale = !eng && !holding && ageH > 36; // last recorded signal is days old and nothing is open

  // Active engine signal or a recent recorded one -> show it as the headline.
  if (eng || (latest && !stale)) {
    const src = eng || latest;
    const isExit = !eng && latest === exit;
    const dec = priceDec(src.entry || src.price || 1);
    const dir = src.direction || "";
    const tagClass = isExit ? "exit" : dir;
    const tagText = isExit ? "EXIT" : dir;
    const when = eng ? "open now (live)" : (latest ? timeAgo(latest.ts) : "");
    const lv = src.entry
      ? `@ <span class="lv">${fmtNum(src.entry, dec)}</span> · SL ${fmtNum(src.stop_loss, dec)} · TP1 ${fmtNum(src.take_profit_1, dec)} · TP2 ${fmtNum(src.take_profit_2, dec)}`
      : src.price != null ? `@ <span class="lv">${fmtNum(src.price, dec)}</span>` : "";
    return `<div class="sig"><span class="tag ${tagClass}">${tagText}</span> ${src.setup || ""} ${lv}
      <span class="when">· ${when}</span>${posLine}${botLine}</div>`;
  }

  // No fresh signal -> show closest engine setup + missing conditions, and, muted, the
  // last thing that ever happened for this coin.
  const lastMuted = latest
    ? `<div class="when">last: ${latest.kind === "exit" ? (latest.setup || "exit") : (latest.direction || "") + " " + (latest.setup || "")} ${latest.price != null ? "@ " + fmtNum(latest.price, priceDec(latest.price)) : ""} · ${timeAgo(latest.ts)}</div>`
    : "";
  if (diag && diag.closest) {
    const miss = (diag.closest_missing || []).slice(0, 3);
    return `<div class="sig"><span class="tag muted">WAITING</span> ${diag.closest} · missing
      ${miss.length ? miss.map((m) => `<span class="miss">${m}</span>`).join(" · ") : "—"}${lastMuted}${posLine}${botLine}</div>`;
  }
  return `<div class="sig">Not enough indicator data yet.${lastMuted}${posLine}${botLine}</div>`;
}

function sparkSVG(pts, w = 240, h = 26) {
  if (!pts || pts.length < 2) return "";
  const xs = pts.map((p) => p.price).filter((v) => v != null);
  if (xs.length < 2) return "";
  const min = Math.min(...xs), max = Math.max(...xs), span = max - min || 1;
  const step = w / (pts.length - 1);
  let d = "";
  pts.forEach((p, i) => {
    if (p.price == null) return;
    const x = (i * step).toFixed(1);
    const y = (h - ((p.price - min) / span) * (h - 2) - 1).toFixed(1);
    d += (d ? "L" : "M") + x + "," + y;
  });
  const up = xs[xs.length - 1] >= xs[0];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path d="${d}" fill="none" stroke="${up ? "var(--up)" : "var(--down)"}" stroke-width="1.3" vector-effect="non-scaling-stroke"/></svg>`;
}

function renderCard(sym) {
  const el = $("#card-" + sym);
  if (!el) return;
  const c = state.coins.get(sym);
  const p = c.price || {};
  const ind = (c.indicators && c.indicators.snapshot) || {};
  const alerts = (c.indicators && c.indicators.alerts) || [];
  const dec = priceDec(p.price || ind.price || 1);
  const chg = p.change_pct;
  const trend = ind.trend;

  el.classList.toggle("has-signal", hasSignal(c));
  el.classList.toggle("near-signal", !hasSignal(c) && nearSignal(c));

  el.innerHTML = `
    <div class="row1">
      <span class="sym">${c.base}</span><span class="tf">${sym} · ${state.tf}</span>
      <button class="rm" title="Remove from watchlist" data-sym="${sym}">✕</button>
    </div>
    <div class="price" id="px-${sym}">
      ${p.price != null ? fmtNum(p.price, dec) : "–"}
      <span class="chg ${chg >= 0 ? "up" : "down"}">${chg != null ? (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%" : ""}</span>
    </div>
    ${sparkSVG(c.spark)}
    <div class="stats">
      <div class="stat"><b>RSI</b><span>${fmtNum(ind.rsi, 1)}</span>
        <div class="bar"><i style="width:${Math.max(0, Math.min(100, ind.rsi || 0))}%;background:${rsiColor(ind.rsi)}"></i></div></div>
      <div class="stat"><b>ADX</b><span>${fmtNum(ind.adx, 1)}</span>
        <div class="bar"><i style="width:${Math.max(0, Math.min(100, (ind.adx || 0) * 1.6))}%"></i></div></div>
      <div class="stat"><b>MACD hist</b><span class="${(ind.macd_hist || 0) >= 0 ? "up" : "down"}">${fmtNum(ind.macd_hist, 4)}</span></div>
      <div class="stat"><b>Vol / avg20</b><span class="${(ind.vol_ratio || 0) >= 2 ? "down" : ""}">${fmtNum(ind.vol_ratio, 2)}×</span></div>
      <div class="stat"><b>EMA50</b><span>${fmtNum(ind.ema_fast, dec)}</span></div>
      <div class="stat"><b>EMA200</b><span>${fmtNum(ind.ema_slow, dec)}</span></div>
    </div>
    <div class="badges">
      <span class="badge ${trend === "up" ? "good" : "bad"}">${trend === "up" ? "▲ Uptrend" : trend === "down" ? "▼ Downtrend" : "trend ?"}</span>
      ${alerts.map((a) => `<span class="badge ${a.level}">${a.text}</span>`).join("")}
    </div>
    ${sigLine(c)}
  `;
  el.querySelector(".rm").onclick = (e) => {
    e.stopPropagation();
    removeCoin(sym);
  };
}

function updateCardPrice(sym, prev) {
  const c = state.coins.get(sym);
  const el = $("#px-" + sym);
  if (!el || !c.price) return;
  const p = c.price;
  const dec = priceDec(p.price);
  const chg = p.change_pct;
  el.innerHTML = `${fmtNum(p.price, dec)} <span class="chg ${chg >= 0 ? "up" : "down"}">${chg != null ? (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%" : ""}</span>`;
  if (prev != null && p.price !== prev) {
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth;
    el.classList.add(p.price > prev ? "flash-up" : "flash-down");
  }
}

function rsiColor(v) {
  if (v == null) return "var(--muted)";
  if (v >= 70) return "var(--down)";
  if (v <= 30) return "var(--up)";
  return "var(--info)";
}

// ---------------- sort / filter ----------------
function initControls() {
  const sel = $("#sortSel");
  sel.value = view.sort;
  sel.onchange = () => {
    view.sort = sel.value;
    localStorage.setItem("gridSort", view.sort);
    layoutGrid();
  };
  for (const [id, key] of [["#fSignal", "fSignal"], ["#fAdx", "fAdx"], ["#fPos", "fPos"]]) {
    const box = $(id);
    box.checked = view[key];
    box.onchange = () => {
      view[key] = box.checked;
      localStorage.setItem(key, box.checked ? "1" : "0");
      layoutGrid();
    };
  }
}

let _relayoutT;
function scheduleRelayout() {
  clearTimeout(_relayoutT);
  _relayoutT = setTimeout(layoutGrid, 700);
}

function layoutGrid() {
  const syms = [...state.coins.keys()];
  const rows = syms.map((s, idx) => {
    const c = state.coins.get(s);
    const ind = (c.indicators && c.indicators.snapshot) || {};
    return {
      s, idx, c, ind,
      chg: (c.price && c.price.change_pct) ?? null,
      hasSig: hasSignal(c),
      nearSig: nearSignal(c),
      adxZone: ind.adx != null && ind.adx >= 25 && ind.adx < 35,
      pos: !!(c.portal_position || c.bot_position),
    };
  });

  let shown = 0;
  rows.forEach((r) => {
    let ok = true;
    if (view.fSignal && !(r.hasSig || r.nearSig)) ok = false;
    if (view.fAdx && !r.adxZone) ok = false;
    if (view.fPos && !r.pos) ok = false;
    const el = $("#card-" + r.s);
    if (el) el.hidden = !ok;
    if (ok) shown++;
  });
  $("#shownCount").textContent = `${shown}/${rows.length} coin`;

  const keyFns = {
    default: (r) => r.idx,
    change: (r) => -(r.chg ?? -1e9),
    rsi: (r) => -(r.ind.rsi ?? -1),
    adx: (r) => -(r.ind.adx ?? -1),
    signal: (r) => (r.hasSig ? 0 : r.nearSig ? 1 : 2),
  };
  const kf = keyFns[view.sort] || keyFns.default;
  [...rows].sort((a, b) => kf(a) - kf(b) || a.idx - b.idx).forEach((r, i) => {
    const el = $("#card-" + r.s);
    if (el) el.style.order = i;
  });
}

// ---------------- notifications + sound ----------------
function renderBell() {
  const b = $("#btnBell");
  b.textContent = (alertPrefs.enabled ? "🔔" : "🔕") + " Alerts";
  b.classList.toggle("on", alertPrefs.enabled);
}
$("#btnBell").onclick = async () => {
  if (!alertPrefs.enabled) {
    if ("Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (e) {}
    }
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      audioCtx = audioCtx || new AC();
      if (audioCtx.state === "suspended") audioCtx.resume();
    } catch (e) {}
    alertPrefs.enabled = true;
    beep(660);
  } else {
    alertPrefs.enabled = false;
  }
  localStorage.setItem("notifyEnabled", alertPrefs.enabled ? "1" : "0");
  renderBell();
};

function fireAlert(ev) {
  if (!alertPrefs.enabled || !feedLoaded) return;
  const isExit = ev.kind === "exit";
  const dec = priceDec(ev.entry || ev.price || 1);
  const title = isExit
    ? `${ev.symbol} · ${(ev.setup || "EXIT").toUpperCase()}`
    : `${ev.symbol} · ${ev.direction || ""} ${ev.setup || ""}`.trim();
  const body = ev.entry
    ? `Entry ${fmtNum(ev.entry, dec)} · SL ${fmtNum(ev.stop_loss, dec)} · TP1 ${fmtNum(ev.take_profit_1, dec)}`
    : ev.price != null ? `@ ${fmtNum(ev.price, dec)}` : "";
  if ("Notification" in window && Notification.permission === "granted") {
    try { new Notification(title, { body, tag: "sig-" + (ev.id || Date.now()) }); } catch (e) {}
  }
  beep(isExit ? 320 : 680);
}

function beep(freq) {
  if (!alertPrefs.enabled) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    audioCtx = audioCtx || new AC();
    const t = audioCtx.currentTime;
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = "sine";
    o.frequency.value = freq;
    o.connect(g); g.connect(audioCtx.destination);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.38);
    o.start(t); o.stop(t + 0.4);
  } catch (e) {}
}

// ---------------- watchlist ----------------
$("#addBtn").onclick = addCoin;
$("#addInput").addEventListener("keydown", (e) => { if (e.key === "Enter") addCoin(); });

async function addCoin() {
  const base = $("#addInput").value.trim().toUpperCase();
  if (!base) return;
  $("#addMsg").textContent = "checking…";
  const r = await fetch("/api/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base }),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    $("#addMsg").textContent = "✕ " + (e.detail || "error");
    return;
  }
  const data = await r.json();
  $("#addInput").value = "";
  $("#addMsg").textContent = "✓ added " + data.added.symbol;
  setTimeout(() => ($("#addMsg").textContent = ""), 2500);
  syncWatchlist(data.watchlist);
}

async function removeCoin(sym) {
  const r = await fetch("/api/watchlist/" + sym, { method: "DELETE" });
  if (r.ok) syncWatchlist((await r.json()).watchlist);
}

function syncWatchlist(list) {
  const want = new Set(list.map((w) => w.symbol));
  for (const sym of [...state.coins.keys()]) if (!want.has(sym)) state.coins.delete(sym);
  for (const w of list) {
    if (!state.coins.has(w.symbol)) state.coins.set(w.symbol, { symbol: w.symbol, base: w.base, spark: [] });
  }
  renderGrid();
}

// ---------------- feed ----------------
async function loadFeed() {
  const rows = await fetch("/api/signals?limit=60").then((r) => r.json());
  $("#feed").innerHTML = "";
  rows.forEach((ev) => prependFeed(ev, false, true));
  feedLoaded = true;
}

function prependFeed(ev, isNew, append) {
  const dec = priceDec(ev.entry || ev.price || 1);
  const el = document.createElement("div");
  el.className = "item " + (ev.kind === "exit" ? "exit" : "entry") + (isNew ? " new" : "");
  const label = ev.kind === "exit" ? (ev.setup || "exit").toUpperCase() : `${ev.direction || ""} ${ev.setup || ""}`;
  el.innerHTML = `<b>${ev.symbol}</b> · ${label}
    ${ev.price != null ? " @ " + fmtNum(ev.price, dec) : ""}
    ${ev.source === "backfill" ? ' <span class="when">(old)</span>' : ""}
    <div class="when">${timeAgo(ev.ts)}</div>`;
  if (append) $("#feed").appendChild(el);
  else $("#feed").prepend(el);
}

// ---------------- chart modal ----------------
let chart, rsiChart, candleSeries, modalSym = null;
const CHART_TOGGLE_KEYS = ["vol", "sr", "trend", "liq", "div", "rsi", "vp"];
const chartToggleDefault = { vol: 1, sr: 1, trend: 1, liq: 0, div: 1, rsi: 1, vp: 0 };
function chartToggles() {
  const t = {};
  for (const k of CHART_TOGGLE_KEYS) {
    const v = localStorage.getItem("chart_" + k);
    t[k] = v == null ? !!chartToggleDefault[k] : v === "1";
  }
  return t;
}
async function openChart(sym) {
  const c = state.coins.get(sym);
  modalSym = sym;
  $("#modalTitle").textContent = `${c.base} — ${sym} (${state.tf})`;
  $("#histSym").textContent = sym;
  $("#modalBg").classList.add("show");

  const ind = (c.indicators && c.indicators.snapshot) || {};
  $("#mini").innerHTML = `
    <div><b>Price</b> ${fmtNum((c.price && c.price.price) || ind.price, priceDec(ind.price || 1))}</div>
    <div><b>RSI</b> ${fmtNum(ind.rsi, 1)}</div>
    <div><b>MACD</b> ${fmtNum(ind.macd, 4)} / sig ${fmtNum(ind.macd_signal, 4)}</div>
    <div><b>ADX</b> ${fmtNum(ind.adx, 1)}</div>
    <div><b>ATR</b> ${fmtNum(ind.atr, priceDec(ind.price || 1))}</div>
    <div><b>Vol×</b> ${fmtNum(ind.vol_ratio, 2)}</div>
    <div id="miniTrade"></div>`;
  renderMiniTrade(sym);

  renderDiag(c);

  $("#orderflow").innerHTML = `<button id="ofBtn">Load order flow</button>`;
  $("#ofBtn").onclick = () => loadOrderFlow(sym);

  $("#btResult").innerHTML = "";
  await renderBtParams(sym);

  const hist = await fetch(`/api/signals?symbol=${sym}&limit=40`).then((r) => r.json());
  $("#hist").innerHTML = hist.length
    ? hist.map((ev) => {
        const dec = priceDec(ev.entry || ev.price || 1);
        return `<div class="feed"><div class="item ${ev.kind}">
          <b>${(ev.kind || "").toUpperCase()}</b> ${ev.direction || ""} ${ev.setup || ""}
          ${ev.price != null ? "@ " + fmtNum(ev.price, dec) : ""}
          <span class="when">· ${new Date(ev.ts).toLocaleString()}</span></div></div>`;
      }).join("")
    : '<p class="hint">No signals recorded for this coin yet.</p>';

  drawChart(sym);
}

function renderDiag(c) {
  const diag = c.indicators && c.indicators.diagnostics;
  if (!diag) {
    $("#diag").innerHTML = '<p class="hint">Not enough indicator data yet.</p>';
    return;
  }
  $("#diag").innerHTML = diag.setups
    .map((s) => {
      if (!s.enabled) return `<div class="dset"><b>${s.name}</b> <span class="hint">(engine has this setup disabled)</span></div>`;
      const conds = s.conditions
        .map((x) => `<div class="dcond ${x.ok ? "ok" : "no"}">${x.ok ? "✓" : "✗"} ${x.label}<span>${x.detail || ""}</span></div>`)
        .join("");
      return `<div class="dset"><b>${s.name}</b>${s.match ? '<span class="ok">✓ matched — would enter</span>' : ` <span class="hint">${s.missing.length} missing</span>`}${conds}</div>`;
    })
    .join("");
}

async function loadOrderFlow(sym) {
  $("#orderflow").innerHTML = '<p class="hint">loading…</p>';
  try {
    const d = await fetch(`/api/orderflow/${sym}`).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
    const buy = d.taker_buy_pct, sell = d.taker_sell_pct;
    $("#orderflow").innerHTML = `
      <div class="of-bars">
        <div class="buy" style="width:${buy}%">${buy.toFixed(0)}%</div>
        <div class="sell" style="width:${sell}%">${sell.toFixed(0)}%</div>
      </div>
      <div class="mini">
        <div><b>Taker buy / sell (1h)</b> ${buy.toFixed(1)}% / ${sell.toFixed(1)}%</div>
        <div><b>Order-book bid/ask</b> ${d.bid_ask_ratio.toFixed(2)} ${d.bid_ask_ratio > 1 ? "(more resting buy liquidity)" : "(more resting sell liquidity)"}</div>
      </div>
      <button id="ofBtn">Refresh</button>`;
    $("#ofBtn").onclick = () => loadOrderFlow(sym);
  } catch (e) {
    $("#orderflow").innerHTML = `<p class="hint">couldn't load order flow (${e.message})</p><button id="ofBtn">Retry</button>`;
    $("#ofBtn").onclick = () => loadOrderFlow(sym);
  }
}

async function loadEngineParams(force) {
  if (state.engineParams && !force) return state.engineParams;
  state.engineParams = await fetch("/api/engine-params").then((r) => r.json());
  return state.engineParams;
}

function btResolvedFor(sym, ep) {
  const merged = { ...ep.defaults };
  const saved = ep.saved || {};
  Object.assign(merged, saved.default || {}, saved[sym] || {});
  return merged;
}

async function renderBtParams(sym) {
  const ep = await loadEngineParams();
  const cur = btResolvedFor(sym, ep);
  const savedForSym = (ep.saved && ep.saved[sym]) || {};
  const field = (m) => {
    const v = cur[m.key];
    const changed = ep.defaults[m.key] !== v;
    if (m.type === "bool") {
      return `<label class="bt-f ${changed ? "chg" : ""}"><input type="checkbox" data-k="${m.key}" data-t="bool" ${v ? "checked" : ""}/> ${m.label}</label>`;
    }
    const step = m.type === "int" ? "1" : "0.1";
    return `<label class="bt-f ${changed ? "chg" : ""}">${m.label}
      <input type="number" step="${step}" data-k="${m.key}" data-t="${m.type}" value="${v}" data-def="${ep.defaults[m.key]}"/>
      <span class="bt-def">default ${ep.defaults[m.key]}</span></label>`;
  };
  const primary = ep.meta.filter((m) => m.group === "primary").map(field).join("");
  const advanced = ep.meta.filter((m) => m.group === "advanced").map(field).join("");
  $("#btParams").innerHTML = `
    <div class="bt-grid">${primary}</div>
    <details class="bt-adv"><summary class="hint">Advanced settings (EMA / periods / SHORT)</summary>
      <div class="bt-grid">${advanced}</div></details>
    ${Object.keys(savedForSym).length ? `<p class="hint">Saved settings for ${sym}: ${Object.entries(savedForSym).map(([k, v]) => `${k}=${v}`).join(", ")}</p>` : ""}`;

  $("#btBtn").onclick = () => runBacktest(sym);
  $("#btReset").onclick = () => resetBtParams(sym);
  $("#btSave").onclick = () => saveBtParams(sym);
  $("#btMsg").textContent = "";
}

function collectBtOverrides() {
  const out = {};
  for (const el of document.querySelectorAll("#btParams [data-k]")) {
    const k = el.dataset.k, t = el.dataset.t;
    let v = t === "bool" ? el.checked : el.value.trim();
    if (t !== "bool" && v === "") continue;
    v = t === "bool" ? v : Number(v);
    if (t !== "bool" && isNaN(v)) continue;
    out[k] = v;
  }
  return out; // server drops values equal to default
}

async function saveBtParams(sym) {
  const values = collectBtOverrides();
  await fetch("/api/engine-params", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope: sym, values }),
  });
  await loadEngineParams(true);
  $("#btMsg").textContent = "✓ saved for " + sym;
  renderBtParams(sym);
}

async function resetBtParams(sym) {
  await fetch("/api/engine-params/" + sym, { method: "DELETE" });
  await loadEngineParams(true);
  $("#btMsg").textContent = "cleared saved settings for " + sym;
  renderBtParams(sym);
}

function btCol(s) {
  if (!s || !s.trades) return `<td class="hint">0 trades</td>`;
  const pf = s.profit_factor == null ? "∞" : s.profit_factor.toFixed(2);
  return `<td>
    <div>${s.trades} trades · win ${s.win_rate}%</div>
    <div>Total <b class="${s.total_r >= 0 ? "up" : "down"}">${s.total_r >= 0 ? "+" : ""}${s.total_r}R</b>
      · avg ${s.avg_r >= 0 ? "+" : ""}${s.avg_r}R</div>
    <div class="hint">PF ${pf} · max DD ${s.max_drawdown_r}R</div>
  </td>`;
}

async function runBacktest(sym) {
  const days = Number($("#btDays").value);
  const overrides = collectBtOverrides();
  const btn = $("#btBtn");
  btn.disabled = true;
  $("#btResult").innerHTML = `<p class="hint">running ${days}-day backtest (default + yours)… a few seconds</p>`;
  try {
    const d = await fetch(`/api/backtest/${sym}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days, params: overrides, compare: true }),
    }).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });

    const tuned = d.tuned;
    const base = d.default; // null when no overrides (tuned == default run)
    const changed = Object.entries(d.overrides || {});
    const rows = tuned.rows || [];
    const tradeRows = rows
      .map((t) => `<tr><td>${t.setup}</td><td>${t.direction}</td>
        <td>${fmtNum(t.entry_price, priceDec(t.entry_price))}</td>
        <td>RSI ${t.entry_rsi} · ADX ${t.entry_adx}</td><td>${t.hold_candles} candles</td>
        <td class="${t.r_multiple >= 0 ? "up" : "down"}">${t.r_multiple >= 0 ? "+" : ""}${t.r_multiple}R</td>
        <td class="hint">${(t.exit_reasons || []).join("+")}</td></tr>`)
      .join("");

    $("#btResult").innerHTML = `
      ${changed.length ? `<p class="hint">Changed: ${changed.map(([k, v]) => `<b>${k}</b>=${v}`).join(", ")}</p>`
        : `<p class="hint">No settings changed — both columns will be identical.</p>`}
      <table class="cfg bt-cmp">
        <tr><th></th><th>Default</th><th>Yours</th></tr>
        <tr><th>${days}d result</th>${btCol(base || tuned)}${btCol(tuned)}</tr>
      </table>
      <details><summary class="hint">${rows.length} trades in detail (your settings)</summary>
        <div style="overflow-x:auto"><table class="cfg">
        <tr><th>setup</th><th>dir</th><th>entry</th><th>at entry</th><th>held</th><th>R</th><th>exit</th></tr>
        ${tradeRows || '<tr><td colspan="7" class="hint">no trades</td></tr>'}</table></div>
      </details>
      <p class="hint">Small sample, no fees modeled — directional evidence, not proof. "Yours" is a what-if only, it isn't applied to the bot.</p>`;
  } catch (e) {
    $("#btResult").innerHTML = `<p class="hint">backtest error (${e.message})</p>`;
  } finally {
    btn.disabled = false;
  }
}

async function drawChart(sym) {
  const box = $("#chart");
  box.innerHTML = '<p class="hint">loading chart…</p>';
  try {
    const [d, evs] = await Promise.all([
      fetch(`/api/chart/${sym}?limit=400`).then((r) => r.json()),
      fetch(`/api/signals?symbol=${sym}&limit=60`).then((r) => r.json()).catch(() => []),
    ]);
    state.chartData = d;
    state.chartSignals = evs || [];
    renderChartToggles();
    renderChart();
  } catch (e) {
    box.innerHTML = `<p class="hint">chart error (${e.message})</p>`;
  }
}

const LS = () => LightweightCharts.LineStyle;
function renderChartToggles() {
  const t = chartToggles();
  const labels = { vol: "Volume", sr: "S/R zones", trend: "Trendlines", liq: "Liquidity", div: "Divergence", rsi: "RSI", vp: "Vol profile" };
  $("#chartToggles").innerHTML = CHART_TOGGLE_KEYS
    .map((k) => `<label><input type="checkbox" data-ct="${k}" ${t[k] ? "checked" : ""}/> ${labels[k]}</label>`)
    .join("");
  $("#chartToggles").querySelectorAll("[data-ct]").forEach((el) => {
    el.onchange = () => { localStorage.setItem("chart_" + el.dataset.ct, el.checked ? "1" : "0"); renderChart(); };
  });
}

function renderChart() {
  const d = state.chartData;
  const box = $("#chart");
  box.innerHTML = "";
  if (rsiChart) { try { rsiChart.remove(); } catch (e) {} rsiChart = null; }
  if (typeof LightweightCharts === "undefined") {
    box.innerHTML = '<p class="hint">Chart library failed to load (offline?).</p>';
    $("#rsiChart").hidden = true;
    return;
  }
  if (!d || !d.candles || !d.candles.length) { box.innerHTML = '<p class="hint">no candle data</p>'; return; }
  const T = chartToggles();

  chart = LightweightCharts.createChart(box, {
    layout: { background: { color: "#161b22" }, textColor: "#8b949e" },
    grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
    rightPriceScale: { borderColor: "#2b3240" },
    timeScale: { borderColor: "#2b3240", timeVisible: true },
    height: 380,
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: "#3fb950", downColor: "#f85149", borderVisible: false,
    wickUpColor: "#3fb950", wickDownColor: "#f85149",
  });
  candleSeries.setData(d.candles.map((k) => ({ time: k.t, open: k.o, high: k.h, low: k.l, close: k.c })));

  if (d.ema) {
    const ef = chart.addLineSeries({ color: "#58a6ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    const es = chart.addLineSeries({ color: "#d29922", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    ef.setData((d.ema.fast || []).map((p) => ({ time: p.t, value: p.value })));
    es.setData((d.ema.slow || []).map((p) => ({ time: p.t, value: p.value })));
  }

  if (T.vol) {
    const vs = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol" });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    vs.setData(d.candles.map((k) => ({ time: k.t, value: k.v, color: k.c >= k.o ? "rgba(63,185,80,.35)" : "rgba(248,81,73,.35)" })));
  }

  if (T.sr) for (const z of d.sr_zones || []) {
    candleSeries.createPriceLine({
      price: z.mid, color: z.kind === "resistance" ? "#f85149" : "#3fb950",
      lineWidth: Math.min(3, Math.max(1, Math.round(z.touches / 4))),
      lineStyle: LS().Dashed, axisLabelVisible: true,
      title: `${z.kind === "resistance" ? "R" : "S"} ×${z.touches}`,
    });
  }

  if (T.vp && d.volume_profile && d.volume_profile.poc) {
    const vp = d.volume_profile;
    candleSeries.createPriceLine({ price: vp.poc, color: "#e3b341", lineWidth: 2, lineStyle: LS().Solid, axisLabelVisible: true, title: "POC" });
    candleSeries.createPriceLine({ price: vp.value_area_high, color: "rgba(227,179,65,.5)", lineWidth: 1, lineStyle: LS().Dotted, axisLabelVisible: false, title: "VAH" });
    candleSeries.createPriceLine({ price: vp.value_area_low, color: "rgba(227,179,65,.5)", lineWidth: 1, lineStyle: LS().Dotted, axisLabelVisible: false, title: "VAL" });
  }

  if (T.trend) for (const tl of d.trendlines || []) {
    const col = tl.broken ? "rgba(248,81,73,.55)" : tl.kind === "up" ? "#3fb950" : "#f85149";
    const s = chart.addLineSeries({ color: col, lineWidth: 1, lineStyle: tl.broken ? LS().Dotted : LS().Solid, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    s.setData(tl.points.map((p) => ({ time: p.t, value: p.price })));
  }

  const markers = [];
  if (T.liq) {
    for (const sw of (d.liquidity && d.liquidity.swings) || [])
      markers.push({ time: sw.t, position: sw.kind === "high" ? "aboveBar" : "belowBar", color: "#6e7681", shape: "circle", text: "" });
    for (const pool of (d.liquidity && d.liquidity.pools) || [])
      candleSeries.createPriceLine({ price: pool.price, color: "rgba(139,148,158,.55)", lineWidth: 1, lineStyle: LS().Dashed, axisLabelVisible: false, title: `liq ${pool.kind} ×${pool.count}` });
  }
  if (T.div) for (const dv of d.divergences || []) {
    const col = dv.kind === "bearish" ? "#f85149" : "#3fb950";
    const b = dv.price[1];
    markers.push({ time: b.t, position: dv.kind === "bearish" ? "aboveBar" : "belowBar", color: col, shape: dv.kind === "bearish" ? "arrowDown" : "arrowUp", text: "div " + (dv.kind === "bearish" ? "▼" : "▲") });
    const ds = chart.addLineSeries({ color: col, lineWidth: 1, lineStyle: LS().LargeDashed, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    ds.setData(dv.price.map((p) => ({ time: p.t, value: p.v })));
  }
  for (const e of state.chartSignals || []) {
    if (e.price == null) continue;
    markers.push({
      time: Math.floor(new Date(e.ts).getTime() / 1000),
      position: e.kind === "exit" ? "aboveBar" : "belowBar",
      color: e.kind === "exit" ? "#d29922" : "#3fb950",
      shape: e.kind === "exit" ? "arrowDown" : "arrowUp",
      text: (e.setup || e.kind || "").slice(0, 12),
    });
  }
  if (markers.length) {
    const seen = new Set();
    candleSeries.setMarkers(
      markers.sort((a, b) => a.time - b.time).filter((m) => {
        const k = m.time + m.position + m.shape;
        return seen.has(k) ? false : seen.add(k);
      })
    );
  }

  if (T.rsi && d.rsi && d.rsi.length) {
    $("#rsiChart").hidden = false;
    rsiChart = LightweightCharts.createChart($("#rsiChart"), {
      layout: { background: { color: "#161b22" }, textColor: "#8b949e" },
      grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
      rightPriceScale: { borderColor: "#2b3240" },
      timeScale: { borderColor: "#2b3240", visible: false },
      height: 120,
    });
    const rs = rsiChart.addLineSeries({ color: "#58a6ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: true });
    rs.setData(d.rsi.map((p) => ({ time: p.t, value: p.value })));
    rs.createPriceLine({ price: 70, color: "rgba(248,81,73,.4)", lineStyle: LS().Dashed, axisLabelVisible: true, title: "70" });
    rs.createPriceLine({ price: 30, color: "rgba(63,185,80,.4)", lineStyle: LS().Dashed, axisLabelVisible: true, title: "30" });
    if (T.div) for (const dv of d.divergences || []) {
      const col = dv.kind === "bearish" ? "#f85149" : "#3fb950";
      const rl = rsiChart.addLineSeries({ color: col, lineWidth: 1, lineStyle: LS().LargeDashed, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      rl.setData(dv.rsi.map((p) => ({ time: p.t, value: p.v })));
    }
    const link = (a, b) => a.timeScale().subscribeVisibleLogicalRangeChange((r) => { if (r) try { b.timeScale().setVisibleLogicalRange(r); } catch (e) {} });
    link(chart, rsiChart); link(rsiChart, chart);
  } else {
    $("#rsiChart").hidden = true;
  }

  chart.timeScale().fitContent();
}

$("#modalClose").onclick = () => {
  $("#modalBg").classList.remove("show");
  modalSym = null;
  if (chart) { try { chart.remove(); } catch (e) {} chart = null; }
  if (rsiChart) { try { rsiChart.remove(); } catch (e) {} rsiChart = null; }
};
$("#modalBg").onclick = (e) => { if (e.target.id === "modalBg") $("#modalClose").onclick(); };

// ---------------- config modal ----------------
$("#btnCfg").onclick = async () => {
  const cfg = await fetch("/api/config").then((r) => r.json());
  state.cfg = cfg.config;
  $("#cfgNote").textContent = cfg.note;
  const syms = [...state.coins.keys()];
  const rows = cfg.keys
    .map((k) => {
      const def = (state.cfg.default && state.cfg.default[k]) ?? cfg.defaults[k];
      const perSym = syms
        .map((s) => {
          const v = state.cfg[s] && state.cfg[s][k];
          return `<td><input data-scope="${s}" data-key="${k}" value="${v ?? ""}" placeholder="${def}"/></td>`;
        })
        .join("");
      return `<tr><th>${k}</th><td><input data-scope="default" data-key="${k}" value="${def}"/></td>${perSym}</tr>`;
    })
    .join("");
  $("#cfgBox").innerHTML = `
    <div style="overflow-x:auto">
    <table class="cfg">
      <tr><th>key</th><th>default</th>${syms.map((s) => `<th>${s}</th>`).join("")}</tr>
      ${rows}
    </table></div>
    <p class="hint">Blank = use the default. The bot engine uses: RSI ${state.engineDefaults.rsi_period}, EMA ${state.engineDefaults.ema_fast}/${state.engineDefaults.ema_slow}, ADX gate ${state.engineDefaults.adx_trend_min}–${state.engineDefaults.adx_trend_max} (not editable here).</p>
    <button id="cfgSave">Save &amp; apply now</button>
    <span id="cfgMsg" class="meta"></span>`;
  $("#cfgSave").onclick = saveCfg;
  $("#cfgBg").classList.add("show");
};
$("#cfgClose").onclick = () => $("#cfgBg").classList.remove("show");
$("#cfgBg").onclick = (e) => { if (e.target.id === "cfgBg") $("#cfgBg").classList.remove("show"); };

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($("#modalBg").classList.contains("show")) $("#modalClose").onclick();
  $("#cfgBg").classList.remove("show");
});

async function saveCfg() {
  const inputs = [...document.querySelectorAll("#cfgBox input")];
  const byScope = {};
  for (const inp of inputs) {
    const v = inp.value.trim();
    if (v === "") continue;
    const num = Number(v);
    if (isNaN(num)) continue;
    (byScope[inp.dataset.scope] ||= {})[inp.dataset.key] = num;
  }
  for (const [scope, values] of Object.entries(byScope)) {
    await fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope, values }),
    });
  }
  $("#cfgMsg").textContent = "✓ saved — applies on the next scan";
  state.cfg = (await fetch("/api/config").then((r) => r.json())).config;
}

// ---------------- trading ----------------
let _tradeTimer = null;
const money = (v, d = 2) => (v == null || isNaN(v) ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d));

async function refreshTradeBadge() {
  try {
    const st = await fetch("/api/trading/status").then((r) => r.json());
    state.trading = st;
    const b = $("#tradeBadge");
    const openN = (st.positions || []).length;
    const uPnl = (st.positions || []).reduce((s, p) => s + (p.unrealized_pnl || 0), 0);
    const day = st.realized_pnl_today_usdt || 0;
    b.hidden = false;
    b.className = "trade-badge " + (st.mode === "live" ? "live" : "dry") + (st.kill_switch ? " killed" : "");
    b.textContent =
      `${st.mode === "live" ? "LIVE" : "DRY"}` +
      (st.kill_switch ? " ⛔" : "") +
      ` · ${openN} pos` +
      (openN ? ` (${money(uPnl)}U)` : "") +
      ` · day ${money(day)}U`;
    if ($("#tradeBg").classList.contains("show")) renderTrade(st);
  } catch (e) {
    $("#tradeBadge").hidden = true;
  }
}

$("#btnTrade").onclick = async () => {
  $("#tradeBg").classList.add("show");
  await refreshTradeBadge();
  renderTrade(state.trading || {});
  clearInterval(_tradeTimer);
  _tradeTimer = setInterval(refreshTradeBadge, 5000);
};
$("#tradeClose").onclick = () => {
  $("#tradeBg").classList.remove("show");
  clearInterval(_tradeTimer);
};
$("#tradeBg").onclick = (e) => { if (e.target.id === "tradeBg") $("#tradeClose").onclick(); };

function renderTrade(st) {
  if (!st || !st.limits) return;
  $("#tradeModeTag").textContent = st.mode === "live" ? "LIVE" : "DRY-RUN";
  $("#tradeModeTag").className = "mode-tag " + (st.mode === "live" ? "live" : "dry");

  const lim = st.limits;
  const readyLine =
    st.mode === "live"
      ? `Anthropic key ${st.anthropic_configured ? "✓" : "✗"} · MCP OAuth ${st.oauth_configured ? "✓" : "✗"} · model ${st.model}` +
        (st.live_ready ? "" : ' — <b class="down">not ready</b>, set the env vars (see docs/trading.md)')
      : "Simulated fills at the reference price minus a taker fee. No network, no order.";
  $("#tradeStatus").innerHTML = `
    <div class="mini">
      <div><b>Mode</b> ${st.mode}</div>
      <div><b>Orders today</b> ${st.orders_today} / ${lim.max_orders_per_day}</div>
      <div><b>Realised P/L today</b> <span class="${(st.realized_pnl_today_usdt||0) >= 0 ? "up" : "down"}">${money(st.realized_pnl_today_usdt)} USDT</span></div>
      <div><b>Per-order cap</b> ${lim.max_notional_per_order_usdt} USDT</div>
      <div><b>Daily loss limit</b> ${lim.daily_loss_limit_usdt} USDT ${st.daily_loss_tripped ? '· <b class="down">TRIPPED</b>' : ""}</div>
      <div><b>Max positions</b> ${lim.max_open_positions}</div>
    </div>
    <div class="trade-kill">
      <label><input type="checkbox" id="killSw" ${st.kill_switch_manual ? "checked" : ""} ${st.kill_switch_env ? "disabled" : ""}/>
        Kill switch (blocks new OPENs; CLOSE always allowed)</label>
      ${st.kill_switch_env ? '<span class="meta">forced on by KILL_SWITCH env</span>' : ""}
    </div>
    <p class="hint">${readyLine}</p>`;
  const ks = $("#killSw");
  if (ks && !st.kill_switch_env) ks.onchange = async () => {
    await fetch("/api/trading/kill-switch", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: ks.checked }),
    });
    refreshTradeBadge();
  };

  const pos = st.positions || [];
  $("#tradePositions").innerHTML = pos.length
    ? `<div style="overflow-x:auto"><table class="cfg">
        <tr><th>coin</th><th>qty</th><th>entry</th><th>mark</th><th>uPnL</th><th></th></tr>
        ${pos.map((p) => {
          const dec = priceDec(p.entry_px);
          return `<tr>
            <td>${p.symbol} <span class="hint">${p.mode}</span></td>
            <td>${p.base_qty.toPrecision(6)}</td>
            <td>${fmtNum(p.entry_px, dec)}</td>
            <td>${p.mark != null ? fmtNum(p.mark, dec) : "–"}</td>
            <td class="${(p.unrealized_pnl||0) >= 0 ? "up" : "down"}">${money(p.unrealized_pnl)} U${p.unrealized_pnl_pct != null ? ` (${money(p.unrealized_pnl_pct,1)}%)` : ""}</td>
            <td><button class="ghost" data-close="${p.symbol}">Close</button></td>
          </tr>`;
        }).join("")}
      </table></div>`
    : '<p class="hint">No open positions.</p>';
  $("#tradePositions").querySelectorAll("[data-close]").forEach((btn) => {
    btn.onclick = () => placeOrder(btn.dataset.close, "CLOSE");
  });

  // coin dropdown = watchlist
  const sel = $("#tradeSym");
  const cur = sel.value;
  sel.innerHTML = [...state.coins.keys()].map((s) => `<option value="${s}">${s}</option>`).join("");
  if (cur) sel.value = cur;
  if (!$("#tradeNotional").value) $("#tradeNotional").value = st.notional_usdt;

  $("#tradeHistory").innerHTML = (st.trades || []).length
    ? `<div style="overflow-x:auto"><table class="cfg">
        <tr><th>coin</th><th>entry</th><th>exit</th><th>P/L</th><th>when</th></tr>
        ${st.trades.map((t) => {
          const dec = priceDec(t.entry_px);
          return `<tr><td>${t.symbol} <span class="hint">${t.mode}</span></td>
            <td>${fmtNum(t.entry_px, dec)}</td><td>${fmtNum(t.exit_px, dec)}</td>
            <td class="${t.realized_pnl >= 0 ? "up" : "down"}">${money(t.realized_pnl)} U</td>
            <td class="hint">${timeAgo(t.closed_at)}</td></tr>`;
        }).join("")}
      </table></div>`
    : '<p class="hint">No closed trades yet.</p>';

  $("#tradeAudit").innerHTML = (st.audit || []).length
    ? `<div style="overflow-x:auto"><table class="cfg">
        <tr><th>when</th><th>intent</th><th>coin</th><th>status</th><th>detail</th></tr>
        ${st.audit.map((a) => {
          let detail = a.error || a.text || "";
          if (a.guardrail) { try { const g = JSON.parse(a.guardrail); if (!g.ok) detail = g.reasons.join("; "); } catch (e) {} }
          if (a.tool_calls) { try { detail = JSON.parse(a.tool_calls).map((c) => c.name + (c.is_error ? "✗" : "✓")).join(", ") + (detail ? " · " + detail : ""); } catch (e) {} }
          return `<tr><td class="hint">${timeAgo(a.ts)}</td><td>${a.intent}</td><td>${a.symbol}</td>
            <td class="${["executed","dry-run"].includes(a.status) ? "up" : (a.status === "blocked" || a.status === "error" || a.status === "refused" ? "down" : "")}">${a.status}</td>
            <td class="hint">${(detail || "").slice(0, 120)}</td></tr>`;
        }).join("")}
      </table></div>`
    : '<p class="hint">No agent calls yet.</p>';
}

function renderMiniTrade(sym) {
  const el = $("#miniTrade");
  if (!el) return;
  const st = state.trading;
  if (!st || !st.limits) { el.innerHTML = ""; return; }
  const pos = (st.positions || []).find((p) => p.symbol === sym);
  const n = st.notional_usdt;
  if (pos) {
    el.innerHTML = `<button class="ghost" id="miniClose">Close ${sym} (${money(pos.unrealized_pnl)} U)</button>`;
    $("#miniClose").onclick = () => placeOrder(sym, "CLOSE");
  } else {
    el.innerHTML = `<button id="miniBuy">${st.mode === "live" ? "Execute" : "Dry-run"} BUY ${n} USDT</button>`;
    $("#miniBuy").onclick = () => placeOrder(sym, "OPEN", n);
  }
}

$("#tradeBuy").onclick = () => {
  const sym = $("#tradeSym").value;
  const n = Number($("#tradeNotional").value);
  if (!sym || !n || n <= 0) { $("#tradeMsg").textContent = "pick a coin and a USDT amount"; return; }
  placeOrder(sym, "OPEN", n);
};

async function placeOrder(symbol, intent, notional) {
  const st = state.trading || {};
  const live = st.mode === "live";
  const price = (state.coins.get(symbol)?.price?.price) ?? null;
  const dec = price ? priceDec(price) : 2;
  const lines = [];
  if (intent === "OPEN") {
    lines.push(`<b>${live ? "LIVE " : "DRY-RUN "}MARKET BUY</b> · ${symbol}`);
    lines.push(`Spend ~<b>${notional} USDT</b>${price ? ` at ~${fmtNum(price, dec)}` : ""}`);
  } else {
    const p = (st.positions || []).find((x) => x.symbol === symbol);
    lines.push(`<b>${live ? "LIVE " : "DRY-RUN "}MARKET SELL</b> · ${symbol}`);
    if (p) lines.push(`Close ~<b>${p.base_qty.toPrecision(6)}</b>${price ? ` at ~${fmtNum(price, dec)}` : ""} · uPnL ${money(p.unrealized_pnl)} U`);
  }
  lines.push(live
    ? '<span class="down">This places a REAL spot order on your Binance sub-account via the MCP agent.</span>'
    : '<span class="hint">Simulated only — no real order, no network.</span>');

  const ok = await askConfirm({
    title: (live ? "Confirm LIVE order" : "Confirm dry-run order"),
    bodyHtml: lines.map((l) => `<div>${l}</div>`).join(""),
    danger: live,
  });
  if (!ok) return;

  $("#tradeMsg").textContent = "placing…";
  try {
    const r = await fetch(`/api/trading/order`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, intent, notional_usdt: intent === "OPEN" ? notional : undefined, confirm: true }),
    });
    const d = await r.json();
    if (!r.ok) { $("#tradeMsg").textContent = "✕ " + (d.detail || "failed"); return; }
    if (d.status === "blocked") $("#tradeMsg").textContent = "⛔ blocked: " + (d.reasons || []).join("; ");
    else if (d.status === "refused") $("#tradeMsg").textContent = "agent refused: " + (d.text || "");
    else if (d.status === "no-op") $("#tradeMsg").textContent = "agent placed nothing: " + (d.text || "");
    else if (d.status === "error") $("#tradeMsg").textContent = "✕ " + (d.error || "error");
    else $("#tradeMsg").textContent = "✓ " + (d.note || d.status) + (d.realized_pnl != null ? ` · P/L ${money(d.realized_pnl)} U` : "");
  } catch (e) {
    $("#tradeMsg").textContent = "✕ " + e.message;
  }
  refreshTradeBadge();
  setTimeout(() => ($("#tradeMsg").textContent = ""), 8000);
}

function askConfirm({ title, bodyHtml, danger }) {
  return new Promise((resolve) => {
    $("#confirmTitle").textContent = title;
    $("#confirmBody").innerHTML = bodyHtml;
    const ok = $("#confirmOk"), cancel = $("#confirmCancel");
    ok.className = danger ? "danger" : "";
    ok.textContent = danger ? "Place REAL order" : "Confirm";
    $("#confirmBg").classList.add("show");
    const done = (v) => {
      $("#confirmBg").classList.remove("show");
      ok.onclick = cancel.onclick = null;
      resolve(v);
    };
    ok.onclick = () => done(true);
    cancel.onclick = () => done(false);
  });
}

// ---------------- utils ----------------
function timeAgo(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

boot();
