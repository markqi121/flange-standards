#!/usr/bin/env bash
#
# 法兰标准库 · 发布脚本（在 Mac 上执行）
#
#   ./deploy/deploy.sh deploy@SERVER_IP                 # 只更新代码并重启
#   ./deploy/deploy.sh deploy@SERVER_IP --import        # 同时重新导入 flange-standards-data
#   DRY_RUN=1 ./deploy/deploy.sh deploy@SERVER_IP       # 只看看会传什么
#
# 前提：服务器已执行过 deploy/setup-server.sh。
set -euo pipefail

HOST="${1:-}"
IMPORT="${2:-}"
if [[ -z "$HOST" ]]; then
  echo "usage: deploy.sh deploy@SERVER_IP [--import]" >&2
  exit 1
fi

APP_DIR=/srv/flange-std/app
VENV=/srv/flange-std/venv
ENV_FILE=/etc/flange-std/env

SSH_OPTS=(-o BatchMode=yes)
if [[ -n "${SSH_KEY:-}" ]]; then
  SSH_OPTS+=(-i "$SSH_KEY")
fi
RSYNC_SSH="ssh ${SSH_OPTS[*]}"

echo "==> rsync 代码到 $HOST:$APP_DIR"
RSYNC_ARGS=(
  -az --delete
  --exclude '.venv/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude 'db.sqlite3'
  --exclude 'staticfiles/'
  --exclude '.git/'
  --exclude '.DS_Store'
  --exclude '.tmp/'
  --exclude 'docs/images/'
)
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  rsync -e "$RSYNC_SSH" "${RSYNC_ARGS[@]}" --dry-run -v ./ "$HOST:$APP_DIR/"
  exit 0
fi

# 发布前代码快照（回滚用），保留最近 5 份
echo "==> 远端快照当前代码到 /srv/flange-std/releases"
ssh "${SSH_OPTS[@]}" "$HOST" 'bash -s' <<'SNAPSHOT'
set -euo pipefail
mkdir -p /srv/flange-std/releases
if [[ -d /srv/flange-std/app ]]; then
  stamp="$(date +%Y%m%d-%H%M%S)"
  tar -czf "/srv/flange-std/releases/app-$stamp.tar.gz" \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='staticfiles' \
    -C /srv/flange-std app
  ls -1t /srv/flange-std/releases/app-*.tar.gz | tail -n +6 | xargs -r rm -f
  echo "snapshot: app-$stamp.tar.gz"
fi
SNAPSHOT

rsync -e "$RSYNC_SSH" "${RSYNC_ARGS[@]}" ./ "$HOST:$APP_DIR/"

echo "==> 远端安装依赖、迁移、收集静态文件"
ssh "${SSH_OPTS[@]}" "$HOST" APP_DIR="$APP_DIR" VENV="$VENV" ENV_FILE="$ENV_FILE" IMPORT="$IMPORT" 'bash -s' <<'REMOTE'
set -euo pipefail
set -a
. "$ENV_FILE"
set +a
cd "$APP_DIR"

# 上线门：迁移前先备份数据库（SQLite 文件 / PostgreSQL pg_dump），失败即中止
BACKUP_DIR="${BACKUP_DIR:-/srv/flange-std/backups}"
export BACKUP_DIR
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [[ "${DJANGO_DB_ENGINE:-}" == *postgres* ]]; then
  echo "==> 备份 PostgreSQL 到 $BACKUP_DIR/db-$STAMP.sql"
  PGPASSWORD="${DJANGO_DB_PASSWORD:-}" pg_dump \
    -h "${DJANGO_DB_HOST:-127.0.0.1}" -p "${DJANGO_DB_PORT:-5432}" \
    -U "${DJANGO_DB_USER:-}" "${DJANGO_DB_NAME:-flange_std}" \
    > "$BACKUP_DIR/db-$STAMP.sql"
else
  echo "==> 备份 SQLite 到 $BACKUP_DIR/db-$STAMP.sqlite3"
  # 优先用 sqlite3 backup API（一致性更好）；失败再退回 cp
  if ! "$VENV/bin/python" deploy/backup.py; then
    cp "${DJANGO_DB_PATH:-db.sqlite3}" "$BACKUP_DIR/db-$STAMP.sqlite3"
  fi
fi
if [[ -d /var/lib/flange-std/pdf ]]; then
  tar -czf "$BACKUP_DIR/pdf-$STAMP.tar.gz" -C /var/lib/flange-std pdf
fi
echo "==> 备份完成：$BACKUP_DIR/*-$STAMP.*"

"$VENV/bin/pip" install -q -r requirements.txt
"$VENV/bin/python" manage.py migrate --noinput
"$VENV/bin/python" manage.py seed_roles
if [[ "${REQUIRE_REAL_PRICES:-0}" == "1" ]]; then
  "$VENV/bin/python" manage.py check_prices
fi
"$VENV/bin/python" manage.py collectstatic --noinput

if [[ "$IMPORT" == "--import" ]]; then
  "$VENV/bin/python" manage.py import_standards
fi

sudo systemctl restart flange-std
sleep 2
echo "==> 健康检查"
curl -fsSL http://127.0.0.1:8011/healthz
echo
REMOTE

echo "==> 发布完成：请用浏览器打开 SITE_URL 检查首页、规格卡和打印页。"
