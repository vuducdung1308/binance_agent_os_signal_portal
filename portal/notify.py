"""Best-effort Telegram push for signals the portal detects.

Independent of the bot's own notifier — configured purely from the portal's own
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID so you can point it at a different bot/chat (or
leave it off) without touching the trading bot.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger("portal.notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 12


class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        """Never raises. Returns True on a 200 from Telegram."""
        if not self.enabled:
            return False
        try:
            r = requests.post(
                _API.format(token=self.token),
                json={"chat_id": self.chat_id, "text": text,
                      "disable_web_page_preview": True},
                timeout=_TIMEOUT,
            )
            if r.status_code != 200:
                log.warning("telegram send failed: %s %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:  # network, DNS, timeout...
            log.warning("telegram send error: %s", exc)
            return False


def _fmt_price(v: Optional[float]) -> str:
    if v is None:
        return "–"
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:,.4f}"
    return f"{v:.6f}"


def format_signal(ev: dict) -> str:
    """One Telegram message for a signal_event row (as stored/broadcast)."""
    sym = ev.get("symbol", "?")
    setup = ev.get("setup") or ""
    src = ev.get("source") or "portal"
    tag = f"  ·  via {src}" if src != "portal" else ""

    if ev.get("kind") == "exit":
        head = f"🔴 {sym}  EXIT · {setup.upper()}"
        lines = [head, f"Price {_fmt_price(ev.get('price'))}"]
        if ev.get("entry") is not None:
            lines.append(f"from entry {_fmt_price(ev['entry'])}")
        if ev.get("rsi") is not None:
            lines.append(f"RSI {ev['rsi']:.1f}")
        return "\n".join(lines) + tag

    arrow = "🟢" if (ev.get("direction") or "").upper() == "LONG" else "🔻"
    head = f"{arrow} {sym}  {ev.get('direction', '')} · {setup}"
    lines = [head]
    if ev.get("entry") is not None:
        lines.append(f"Entry {_fmt_price(ev['entry'])} · SL {_fmt_price(ev.get('stop_loss'))}")
        lines.append(f"TP1 {_fmt_price(ev.get('take_profit_1'))} · TP2 {_fmt_price(ev.get('take_profit_2'))}")
    elif ev.get("price") is not None:
        lines.append(f"@ {_fmt_price(ev['price'])}")
    ind = []
    if ev.get("rsi") is not None:
        ind.append(f"RSI {ev['rsi']:.1f}")
    if ev.get("adx") is not None:
        ind.append(f"ADX {ev['adx']:.1f}")
    if ind:
        lines.append(" · ".join(ind))
    return "\n".join(lines) + tag
