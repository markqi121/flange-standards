#!/usr/bin/env bash
#
# 法兰标准库 · 首次服务器初始化（Ubuntu 22.04 / 24.04）
#
# 前提：已经用 rsync 把代码放进 /srv/flange-std/app（deploy/deploy.sh 第一次也会做）。
# 用法（在服务器上，root）：
#   bash /srv/flange-std/app/deploy/setup-server.sh std.example.com
#
# 做的事：装 Python/Nginx/证书工具、建 deploy 用户、建 venv、写 env 文件、
# 安装 systemd 服务与 Nginx 站点配置、reload Nginx。不会启动应用（等首次 deploy）。
set -euo pipefail

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
  echo "usage: setup-server.sh <domain>" >&2
  exit 1
fi
if [[ "$(id -u)" != "0" ]]; then
  echo "error: run as root" >&2
  exit 1
fi

APP_DIR=/srv/flange-std/app
VENV_DIR=/srv/flange-std/venv
DATA_DIR=/var/lib/flange-std
ENV_FILE=/etc/flange-std/env

if [[ ! -f "$APP_DIR/manage.py" ]]; then
  echo "error: $APP_DIR/manage.py 不存在；先用 rsync 上传代码（见 docs/部署指南.md）" >&2
  exit 1
fi

echo "==> 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx rsync curl certbot python3-certbot-nginx

echo "==> 创建 deploy 用户与目录"
if ! id deploy >/dev/null 2>&1; then
  adduser --system --group --home /home/deploy --shell /bin/bash deploy
fi
mkdir -p "$DATA_DIR" /etc/flange-std /home/deploy /home/deploy/.ssh
chown -R deploy:deploy /srv/flange-std "$DATA_DIR" /home/deploy

# 把 root 的 SSH 公钥复制给 deploy，方便后续用 deploy 用户发布
if [[ -f /root/.ssh/authorized_keys ]]; then
  cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
  chown -R deploy:deploy /home/deploy/.ssh
  chmod 700 /home/deploy/.ssh
  chmod 600 /home/deploy/.ssh/authorized_keys
fi

echo "==> 创建虚拟环境并安装依赖"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi
chown -R deploy:deploy "$VENV_DIR"
sudo -u deploy "$VENV_DIR/bin/pip" install -q --upgrade pip
sudo -u deploy "$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> 安装 PDF 生成用的 Chromium（约 700 MB）"
export PLAYWRIGHT_BROWSERS_PATH=/var/lib/flange-std/ms-playwright
sudo -u deploy env PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH" \
  "$VENV_DIR/bin/playwright" install chromium
"$VENV_DIR/bin/playwright" install-deps chromium
mkdir -p /var/lib/flange-std/pdf
chown -R deploy:deploy /var/lib/flange-std

echo "==> 写环境变量 $ENV_FILE"
if [[ ! -f "$ENV_FILE" ]]; then
  SECRET=$(openssl rand -hex 32)
  cat > "$ENV_FILE" <<ENVVARS
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=$SECRET
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,$DOMAIN
DJANGO_CSRF_TRUSTED_ORIGINS=https://$DOMAIN
DJANGO_HTTPS=1
SITE_URL=https://$DOMAIN
DJANGO_DB_PATH=$DATA_DIR/db.sqlite3
PLAYWRIGHT_BROWSERS_PATH=$DATA_DIR/ms-playwright
PDF_CACHE_DIR=$DATA_DIR/pdf
PDF_RENDER_BASE_URL=https://$DOMAIN
PDF_LAYOUT_VERSION=1
GUNICORN_WORKERS=3
ENVVARS
  chmod 600 "$ENV_FILE"
  chown deploy:deploy "$ENV_FILE"
else
  echo "    $ENV_FILE 已存在，保留原值（需要改域名/密钥请手动编辑）"
fi

echo "==> 安装 systemd 服务"
install -m 644 "$APP_DIR/deploy/flange-std.service" /etc/systemd/system/flange-std.service
systemctl daemon-reload

echo "==> 安装 Nginx 站点"
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/deploy/nginx-flange-std.conf" \
  > /etc/nginx/sites-available/flange-std
ln -sf /etc/nginx/sites-available/flange-std /etc/nginx/sites-enabled/flange-std
nginx -t
systemctl enable --now nginx
systemctl reload nginx

echo "==> 允许 deploy 重启服务"
SUDO_PATH=$(command -v systemctl)
cat > /etc/sudoers.d/flange-std <<SUDOERS
deploy ALL=(ALL) NOPASSWD: $SUDO_PATH restart flange-std, $SUDO_PATH status flange-std
SUDOERS
chmod 440 /etc/sudoers.d/flange-std

if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null 2>&1 || true
  ufw allow 'Nginx Full' >/dev/null 2>&1 || true
fi

echo
echo "==> 初始化完成。下一步："
echo "  1. 在阿里云控制台防火墙放行 22/80/443"
echo "  2. 域名 $DOMAIN 的 A 记录指向本机"
echo "  3. 在本机执行首次发布（会导入数据）："
echo "       ./deploy/deploy.sh deploy@<服务器IP> --import"
echo "  4. 证书：ssh root@<服务器IP> 'certbot --nginx -d $DOMAIN'"
echo "  5. 建后台账号："
echo "       ssh deploy@<服务器IP> 'cd $APP_DIR && set -a && . $ENV_FILE && set +a && $VENV_DIR/bin/python manage.py createsuperuser'"
