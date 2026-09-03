"""Is the Binansquare bot itself alive? The portal runs independently of the bot's
LaunchAgent schedulers, so this reports on them separately.

`launchctl list` is the reliable source (is the agent loaded, is it running, what was
its last exit code) when the LaunchAgents are installed. Log-file mtime is only a weak
secondary hint: scheduler_watch.py / the news + hot-movers schedulers barely write to
their logs between runs (watch logs only on an event; news/hotmovers run every few
hours), so a "stale" log there is normal, not a failure — we only fall back to it when
launchctl has nothing to say.

Read-only. Nothing here starts, stops, or reloads anything.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Dict

# name -> (log file, seconds after which a *log-only* check is treated as merely "idle")
_AGENTS = {
    "signals": ("scheduler_signals.log", 95 * 60),
    "watch": ("scheduler_watch.log", 8 * 60),
    "news": ("scheduler.log", 5 * 3600),
    "hotmovers": ("scheduler_hot_movers.log", 6 * 3600),
}


def _launchctl() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        res = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
    except Exception:
        return out
    for line in res.stdout.splitlines():
        if "binansquare" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, status, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
        key = label.rsplit(".", 1)[-1]
        running = pid not in ("-", "")
        out[key] = {
            "running": running,
            "pid": int(pid) if running and pid.lstrip("-").isdigit() else None,
            "last_exit": status,
        }
    return out


def bot_health(bot_root: Path) -> Dict[str, Any]:
    logs_dir = bot_root / "logs"
    now = time.time()
    lc = _launchctl()
    agents: Dict[str, Any] = {}

    for name, (fname, idle_after) in _AGENTS.items():
        p = logs_dir / fname
        age = round(now - p.stat().st_mtime) if p.exists() else None

        entry = lc.get(name)
        if entry is not None:
            if not entry["running"]:
                status = "down"          # loaded but not running
            elif entry["last_exit"] not in ("0", "-"):
                status = "crashed"       # running now, but last run exited non-zero
            else:
                status = "ok"
            source = "launchctl"
        elif age is None:
            status, source = "unknown", "none"
        elif age <= idle_after:
            status, source = "ok", "log"
        else:
            status, source = "idle", "log"   # can't confirm; log just hasn't moved

        agents[name] = {
            "log": fname, "age_sec": age, "status": status, "source": source,
            "launchctl": entry,
        }

    if any(a["status"] in ("down", "crashed") for a in agents.values()):
        overall = "problem"
    elif all(a["status"] == "unknown" for a in agents.values()):
        overall = "unknown"
    else:
        overall = "ok"

    return {"overall": overall, "agents": agents,
            "launchctl_available": bool(lc), "checked_at": round(now)}
