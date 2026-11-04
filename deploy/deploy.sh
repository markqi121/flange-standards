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
rsync -e "$RSYNC_SSH" "${RSYNC_ARGS[@]}" ./ "$HOST:$APP_DIR/"

echo "==> 远端安装依赖、迁移、收集静态文件"
ssh "${SSH_OPTS[@]}" "$HOST" APP_DIR="$APP_DIR" VENV="$VENV" ENV_FILE="$ENV_FILE" IMPORT="$IMPORT" 'bash -s' <<'REMOTE'
set -euo pipefail
set -a
. "$ENV_FILE"
set +a
cd "$APP_DIR"

"$VENV/bin/pip" install -q -r requirements.txt
"$VENV/bin/python" manage.py migrate --noinput
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
