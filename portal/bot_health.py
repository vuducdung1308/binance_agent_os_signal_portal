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

# launchctl's "last exit" as -N means the previous run was killed by signal N. These are
# "please stop" signals — they fire on `launchctl unload`/reload, logout, and reboot, and
# a KeepAlive agent just restarts. Not a crash.
_STOP_SIGNALS = {"-1", "-2", "-3", "-15"}  # SIGHUP, SIGINT, SIGQUIT, SIGTERM


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
            last = str(entry["last_exit"])
            if not entry["running"]:
                status = "down"          # loaded but not running right now
            elif last in ("0", "-") or last in _STOP_SIGNALS:
                # running now; last run either exited cleanly or was told to stop
                # (SIGTERM/SIGINT/... — normal on unload/reload/reboot, KeepAlive
                # brought it back). Not a crash.
                status = "ok"
            else:
                # running now, but the previous run died on a crash signal or a
                # non-zero code — flag it amber, not red.
                status = "warn"
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

    if any(a["status"] == "down" for a in agents.values()):
        overall = "problem"
    elif any(a["status"] in ("warn", "idle") for a in agents.values()):
        overall = "warn"
    elif all(a["status"] == "unknown" for a in agents.values()):
        overall = "unknown"
    else:
        overall = "ok"

    return {"overall": overall, "agents": agents,
            "launchctl_available": bool(lc), "checked_at": round(now)}
