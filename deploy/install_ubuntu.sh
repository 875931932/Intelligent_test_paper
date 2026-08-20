#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/intelligent-test-paper}"
REPO_URL="${REPO_URL:-https://gitee.com/yan-ace/zhinengchujuanxitong.git}"
BRANCH="${BRANCH:-master}"

sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip nodejs npm postgresql postgresql-contrib redis-server nginx

if [[ ! -d "$APP_DIR/.git" ]]; then
  sudo git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  sudo git -C "$APP_DIR" fetch origin "$BRANCH"
  sudo git -C "$APP_DIR" checkout "$BRANCH"
  sudo git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
fi

cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./backend
echo "Code and Python dependencies installed. Create $APP_DIR/.env, then run deploy/start.sh."
