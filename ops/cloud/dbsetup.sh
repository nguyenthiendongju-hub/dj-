#!/usr/bin/env bash
# Khởi động Postgres + Redis, đảm bảo role/db tồn tại, chạy seed (idempotent).
# Dùng chung bởi install.sh (bake vào snapshot) và start.sh (mỗi lần boot).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"

# Ubuntu 22.04 cloud = Postgres 14; không hard-code 16
if [ -z "${PG_CLUSTER_VERSION}" ] && [ -d /etc/postgresql ]; then
  PG_CLUSTER_VERSION="$(ls /etc/postgresql | sort -rn | head -1)"
fi
export PG_CLUSTER_VERSION="${PG_CLUSTER_VERSION:-14}"

echo "[dbsetup] Khởi động PostgreSQL cluster ${PG_CLUSTER_VERSION}/main..."
sudo pg_ctlcluster "${PG_CLUSTER_VERSION}" main start 2>/dev/null || true

echo "[dbsetup] Khởi động Redis..."
if ! redis-cli ping >/dev/null 2>&1; then
  sudo redis-server --daemonize yes
fi

# Chờ Postgres sẵn sàng
for i in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done

echo "[dbsetup] Đảm bảo role & database '${DJHRM_DB_NAME}'..."
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${DJHRM_DB_USER}') THEN
    CREATE ROLE ${DJHRM_DB_USER} LOGIN PASSWORD '${DJHRM_DB_PASSWORD}';
  END IF;
END \$\$;
SQL
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DJHRM_DB_NAME}'" | grep -q 1 \
  || sudo -u postgres createdb -O "${DJHRM_DB_USER}" "${DJHRM_DB_NAME}"

echo "[dbsetup] Seed dữ liệu demo (create_all + seed, idempotent)..."
cd "$ROOT/apps/api"
./.venv/bin/python -m app.scripts.seed

echo "[dbsetup] Hoàn tất: Postgres + Redis sẵn sàng, đã seed."
