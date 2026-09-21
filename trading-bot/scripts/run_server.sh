#!/usr/bin/env bash
#
# Start the ApexAlgo control server on Linux or macOS.
#
#   ./run_server.sh              production server (waitress) on 127.0.0.1:8800
#   ./run_server.sh --dev        Flask development server, for local debugging
#
# Environment overrides:
#   APEX_HOST, APEX_PORT, APEX_BEHIND_PROXY, APEX_BOT_TOKEN,
#   APEX_PANEL_PASSWORD, APEX_TELEGRAM_TOKEN, APEX_TELEGRAM_CHATS
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(dirname "$SCRIPT_DIR")/server"
VENV_DIR="$SERVER_DIR/.venv"

cd "$SERVER_DIR"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "==> Creating the virtual environment"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip --quiet
  "$VENV_DIR/bin/pip" install -r requirements.txt
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

HOST="${APEX_HOST:-127.0.0.1}"
PORT="${APEX_PORT:-8800}"

# Generate config.json with fresh secrets on first run, and show them once.
if [[ ! -f config.json ]]; then
  echo "==> First run: generating config.json"
  python -c "from config import Config; Config.load()"
fi

if [[ "${1:-}" == "--dev" ]]; then
  echo "==> Development server on http://$HOST:$PORT"
  echo "    (not for permanent use - run without --dev for waitress)"
  exec python app.py
fi

if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]]; then
  echo "!!  Binding to $HOST, which is reachable from outside this machine."
  echo "!!  Put TLS in front of it - see docs/04-install-vps.md."
fi

echo "==> Control server on http://$HOST:$PORT"
exec python -m waitress --host="$HOST" --port="$PORT" --threads=8 app:create_app
