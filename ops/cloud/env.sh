# DJ HRM — biến môi trường cho Cloud Agent (native, không Docker).
# Được source bởi install.sh, start.sh và các terminal (api/web).
# Postgres + Redis chạy ngay trên VM nên trỏ về 127.0.0.1.

export APP_ENV="${APP_ENV:-local}"
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://djhrm:djhrm_local_change_me@127.0.0.1:5432/djhrm}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
export JWT_SECRET="${JWT_SECRET:-dev_local_jwt_secret_change_me_please_32chars}"
export AGENT_TOKEN="${AGENT_TOKEN:-dev_local_agent_token}"
export CORS_ORIGINS="${CORS_ORIGINS:-http://localhost:5173,http://localhost:3000}"
export TRUSTED_HOSTS="${TRUSTED_HOSTS:-*}"

# Seed admin (đổi mật khẩu sau lần đăng nhập đầu)
export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-Admin@DongJu2026}"

# Web: Vite proxy /api -> API local
export VITE_PROXY_TARGET="${VITE_PROXY_TARGET:-http://localhost:8000}"

# Postgres credentials dùng cho việc tạo role/db
export DJHRM_DB_NAME="${DJHRM_DB_NAME:-djhrm}"
export DJHRM_DB_USER="${DJHRM_DB_USER:-djhrm}"
export DJHRM_DB_PASSWORD="${DJHRM_DB_PASSWORD:-djhrm_local_change_me}"
export PG_CLUSTER_VERSION="${PG_CLUSTER_VERSION:-}"
