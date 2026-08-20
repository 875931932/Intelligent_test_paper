#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
VENV="$ROOT_DIR/.venv"
LOG_DIR="$ROOT_DIR/var/log"
PID_DIR="$ROOT_DIR/var/run"
mkdir -p "$LOG_DIR" "$PID_DIR"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Missing virtualenv: $VENV" >&2
  exit 1
fi
if [[ ! -f "$ROOT_DIR/.env" ]]; then
  echo "Missing $ROOT_DIR/.env" >&2
  exit 1
fi

set -a
source "$ROOT_DIR/.env"
set +a

cd "$BACKEND_DIR"
PYTHONPATH=. "$VENV/bin/python" -m app.db.init_db

start_process() {
  local name="$1"; shift
  local pid_file="$PID_DIR/$name.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pid_file"))"
    return
  fi
  rm -f "$pid_file"
  nohup "$@" >>"$LOG_DIR/$name.log" 2>&1 &
  echo $! >"$pid_file"
  echo "started $name (pid $!)"
}

start_process api "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port "${API_PORT:-8000}"
start_process worker "$VENV/bin/celery" -A app.infrastructure.tasks.celery_app worker --loglevel="${CELERY_LOGLEVEL:-INFO}" --concurrency="${CELERY_CONCURRENCY:-2}"

if [[ "${BUILD_FRONTEND:-1}" == "1" ]]; then
  command -v npm >/dev/null || { echo "npm is required to build frontend" >&2; exit 1; }
  cd "$ROOT_DIR/frontend"
  npm ci --registry="${NPM_REGISTRY:-https://registry.npmmirror.com}"
  npm run build
fi
echo "Services started. API: http://127.0.0.1:${API_PORT:-8000}/api/v1/health"
