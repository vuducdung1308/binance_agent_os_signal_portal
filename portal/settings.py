"""Portal-side configuration. Independent of the bot's own .env — only BOT_ROOT
points back at it so we can import its modules and read its watchlist/timeframe.

All values overridable via environment (or a .env in this folder)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # optional .env in the portal folder

PORTAL_ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class PortalSettings:
    bot_root: Path
    db_path: Path
    static_dir: Path
    host: str
    port: int
    # How often to refresh the live price/volume ticker (cheap: one batched request).
    price_poll_sec: int
    # How often to refetch closed candles + recompute indicators/signals. The bot works
    # on CLOSED candles only (src/price_data.py._drop_unclosed), so indicator values
    # change at most once per candle — polling faster than this just catches the close
    # a little sooner, it does not give "smoother" indicators.
    signal_poll_sec: int
    # How often a full indicator snapshot row is written to SQLite for history/sparklines.
    snapshot_interval_sec: int
    # Delete indicator_snapshot rows older than this many days on startup.
    snapshot_retention_days: int
    timeframe: str
    klines_limit: int
    # Telegram push for portal-detected signals. Both must be set to enable it.
    # Kept separate from the bot's own TELEGRAM_* so you can use a different bot/chat.
    telegram_bot_token: str
    telegram_chat_id: str
    # Also push signals the portal ingested from the bot's output/signals/*.txt.
    # Leave false when BOT_ROOT points at a real bot that already sends its own.
    telegram_include_bot_signals: bool


def load_portal_settings() -> PortalSettings:
    # Defaults to the copy of the bot's engine bundled in this repo (./bot). Point it
    # at your own full Binansquare checkout with BOT_ROOT to track live positions/history.
    bot_root = Path(
        os.environ.get("BOT_ROOT", str(PORTAL_ROOT / "bot"))
    ).expanduser().resolve()

    # Pull the bot's configured timeframe as the default (its .env, read directly —
    # importing src.config would also work but this avoids its load_dotenv side effects).
    timeframe = os.environ.get("PORTAL_TIMEFRAME", "").strip()
    if not timeframe:
        timeframe = _read_bot_env(bot_root, "SIGNAL_TIMEFRAME", "1h")

    return PortalSettings(
        bot_root=bot_root,
        db_path=Path(os.environ.get("PORTAL_DB", PORTAL_ROOT / "data" / "portal.db")).expanduser(),
        static_dir=PORTAL_ROOT / "static",
        host=os.environ.get("PORTAL_HOST", "127.0.0.1"),
        port=_int("PORTAL_PORT", 8777),
        price_poll_sec=_int("PORTAL_PRICE_POLL_SEC", 5),
        signal_poll_sec=_int("PORTAL_SIGNAL_POLL_SEC", 30),
        snapshot_interval_sec=_int("PORTAL_SNAPSHOT_INTERVAL_SEC", 60),
        snapshot_retention_days=_int("PORTAL_SNAPSHOT_RETENTION_DAYS", 30),
        timeframe=timeframe,
        klines_limit=_int("PORTAL_KLINES_LIMIT", 300),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        telegram_include_bot_signals=_bool("PORTAL_TELEGRAM_INCLUDE_BOT", False),
    )


def _read_bot_env(bot_root: Path, key: str, default: str) -> str:
    env_file = bot_root / ".env"
    if not env_file.exists():
        return default
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip() or default
    return default


def bot_watchlist(bot_root: Path) -> list[str]:
    """The bot's SIGNAL_COINS from its .env — used only to seed the portal watchlist
    on first run; after that the portal's SQLite copy is the source of truth."""
    raw = _read_bot_env(bot_root, "SIGNAL_COINS", "BTC,ETH,BNB,ADA,SOL,LTC")
    return [c.strip().upper() for c in raw.split(",") if c.strip()]
