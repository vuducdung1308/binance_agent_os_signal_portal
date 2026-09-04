#!/usr/bin/env bash
# Local dev launcher for the Binance Agent OS Signal Portal.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "· creating $VENV"
  "$PYTHON" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

pip install -q -r requirements.txt

export BOT_ROOT="${BOT_ROOT:-$(pwd)/bot}"
export PORTAL_HOST="${PORTAL_HOST:-127.0.0.1}"
export PORTAL_PORT="${PORTAL_PORT:-8777}"

# A previous portal instance still holding the port would keep serving OLD code while
# your fresh static files load fine — confusing. Stop it first.
OLD_PID="$(lsof -ti "tcp:${PORTAL_PORT}" 2>/dev/null || true)"
if [ -n "$OLD_PID" ]; then
  echo "· port ${PORTAL_PORT} in use by pid ${OLD_PID} — stopping it"
  kill $OLD_PID 2>/dev/null || true
  sleep 1
  lsof -ti "tcp:${PORTAL_PORT}" 2>/dev/null | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo "· portal → http://${PORTAL_HOST}:${PORTAL_PORT}   (bot: ${BOT_ROOT})"
exec python -m portal.app
