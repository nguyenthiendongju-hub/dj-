#!/usr/bin/env bash
# DJ HRM — cài đặt phụ thuộc cho Cloud Agent (chạy native, không Docker).
# Idempotent: có thể chạy lại nhiều lần an toàn.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"

echo "[install] 1/4 Gói hệ thống: PostgreSQL, Redis, python venv..."
need_apt=0
command -v psql >/dev/null 2>&1 || need_apt=1
command -v redis-server >/dev/null 2>&1 || need_apt=1
dpkg -s python3-venv >/dev/null 2>&1 || need_apt=1
if [ "$need_apt" = "1" ]; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    postgresql postgresql-contrib redis-server python3-venv python3-pip
fi

echo "[install] 2/4 Python venv + deps (apps/api)..."
cd "$ROOT/apps/api"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q

echo "[install] 3/4 Node deps (apps/web)..."
cd "$ROOT/apps/web"
npm install --no-fund --no-audit

echo "[install] 4/4 Postgres + Redis + seed (bake vào snapshot)..."
bash "$SCRIPT_DIR/dbsetup.sh"

echo "[install] Xong. API: apps/api (.venv), Web: apps/web (node_modules)."
