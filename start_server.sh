#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if [[ ! -f ".venv/bin/activate" ]]; then
  echo "Error: .venv not found. Create it first: python -m venv .venv"
  exit 1
fi

source ".venv/bin/activate"

if [[ -z "${ROBYN_SECRET_KEY:-}" ]]; then
  ROBYN_SECRET_KEY="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  export ROBYN_SECRET_KEY
  echo "Info: ROBYN_SECRET_KEY was not set; generated a temporary key for this run."
  echo "Info: Set a fixed ROBYN_SECRET_KEY to keep sessions valid across restarts."
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-9009}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

export ROBYN_HOST="${HOST}"
export ROBYN_PORT="${PORT}"

DEV_MODE="${DEV_MODE:-0}"
DEV_FLAG=""
case "${DEV_MODE,,}" in
  1|true|yes|on)
    DEV_FLAG="--dev"
    ;;
esac

if [[ -n "${DEV_FLAG}" ]]; then
  echo "Starting dev server on http://${HOST}:${PORT}"
else
  echo "Starting server on http://${HOST}:${PORT}"
fi

CMD=(robyn app.py)
if [[ -n "${DEV_FLAG}" ]]; then
  CMD+=("${DEV_FLAG}")
fi
CMD+=(--log-level "${LOG_LEVEL}")
exec "${CMD[@]}"
