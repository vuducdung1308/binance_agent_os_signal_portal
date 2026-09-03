"""One-time import of the bot's existing draft files (output/signals/*.txt) into the
signal_event table so the History view has data from before the portal existed.

These files are the only signal history the bot kept — one text file per signal, named
SYMBOL_(entry|exit)_YYYYMMDD_HHMMSS.txt. We parse what we can from the filename and a
couple of lines of the body; anything unparseable is skipped.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List

_NAME_RE = re.compile(r"^([A-Z0-9]+USDT)_(entry|exit)_(\d{8})_(\d{6})\.txt$")
_DIR_RE = re.compile(r"Direction:\s*(LONG|SHORT)", re.I)
_EXIT_HEAD_RE = re.compile(r"^([A-Z0-9 ()\-]+?)\s+—\s+[A-Z0-9]+USDT\s+\((LONG|SHORT)\)", re.M)
_ENTRY_PRICE_RE = re.compile(r"(?:Entry|Điểm vào lệnh):\s*([0-9][0-9,]*\.?[0-9]*)", re.I)
_CUR_PRICE_RE = re.compile(r"(?:Current price|Giá hiện tại):\s*([0-9][0-9,]*\.?[0-9]*)", re.I)

_EXIT_REASON = {
    "TAKE-PROFIT 1": "tp1_hit",
    "TAKE-PROFIT 2": "tp2_hit",
    "STOP-LOSS": "sl_hit",
    "TECHNICAL REVERSAL": "technical_reversal",
}


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_signals_dir(signals_dir: Path) -> List[dict]:
    rows: List[dict] = []
    if not signals_dir.exists():
        return rows

    for path in sorted(signals_dir.glob("*.txt")):
        m = _NAME_RE.match(path.name)
        if not m:
            continue  # MARKET_OVERVIEW_*, x_article_*, etc.
        symbol, kind, ymd, hms = m.groups()
        try:
            # The bot names these files with datetime.now() (system LOCAL time, naive).
            # astimezone() reads a naive datetime as local and converts to UTC.
            ts = (
                datetime.strptime(ymd + hms, "%Y%m%d%H%M%S")
                .astimezone(timezone.utc)
                .isoformat()
            )
        except (ValueError, OSError):
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""

        direction = None
        setup = None
        price = None

        if kind == "entry":
            dm = _DIR_RE.search(text)
            direction = dm.group(1).upper() if dm else None
            pm = _ENTRY_PRICE_RE.search(text)
            price = _num(pm.group(1)) if pm else None
            setup = "long_breakout" if "20-candle high" in text or "phá đỉnh" in text else (
                "long_pullback" if "dip-buy" in text or "pullback" in text else "entry"
            )
        else:  # exit
            hm = _EXIT_HEAD_RE.search(text)
            if hm:
                direction = hm.group(2).upper()
                head = hm.group(1).upper()
                setup = next((v for k, v in _EXIT_REASON.items() if k in head), "exit")
            pm = _CUR_PRICE_RE.search(text)
            price = _num(pm.group(1)) if pm else None

        rows.append(
            {"symbol": symbol, "ts": ts, "kind": kind,
             "direction": direction, "setup": setup, "price": price}
        )
    return rows
