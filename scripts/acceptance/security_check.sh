#!/usr/bin/env bash
# 报价功能 · 安全配置验收（QA 验收 G）
#
#   scripts/acceptance/security_check.sh
#
# 检查：
# 1) 开发默认配置必须被 Django `check --deploy` 标记不安全（说明生产必须改 env）；
# 2) 按 deploy/env.example 的硬化 env 运行 `check --deploy`，不得残留关键项；
# 3) HTTPS=1 时 Cookie/HSTS/代理头配置生效；
# 4) deploy/deploy.sh 是否有"迁移前备份"；
# 5) /admin/ 保护与登录入口存在（静态检查 + Django 路由）。
# 任何关键项失败退出码非 0。
set -uo pipefail
cd "$(dirname "$0")/../.."

PY=".venv/bin/python"
FAIL=0
run() { echo; echo "===== $* ====="; }

run "1. 开发默认配置（应被标记 W009/W018）"
DEV_OUT=$($PY manage.py check --deploy 2>&1)
echo "$DEV_OUT" | tail -12
if echo "$DEV_OUT" | grep -q "security.W018"; then
  echo "[ok] DEBUG 开发默认值被标记"
else
  echo "[!!] 未标记 W018"; FAIL=1
fi
if echo "$DEV_OUT" | grep -q "security.W009"; then
  echo "[ok] 默认 SECRET_KEY 被标记不安全"
else
  echo "[!!] 未标记 W009"; FAIL=1
fi

run "2. 硬化生产 env（DEBUG=0 + 随机 SECRET + ALLOWED_HOSTS + HTTPS=1）"
PROD_OUT=$(DJANGO_DEBUG=0 \
  DJANGO_SECRET_KEY="$(openssl rand -hex 32 2>/dev/null || echo 'qa-secret-0123456789abcdef0123456789abcdef')" \
  DJANGO_ALLOWED_HOSTS="std.example.com,127.0.0.1" \
  DJANGO_CSRF_TRUSTED_ORIGINS="https://std.example.com" \
  DJANGO_HTTPS=1 \
  $PY manage.py check --deploy 2>&1)
PROD_RC=$?
echo "$PROD_OUT" | tail -12
if [ $PROD_RC -ne 0 ]; then echo "[!!] 硬化配置 check 失败"; FAIL=1; fi
for code in security.W009 security.W018 security.W012 security.W016; do
  if echo "$PROD_OUT" | grep -q "$code"; then
    echo "[!!] 硬化配置仍残留 $code"; FAIL=1
  else
    echo "[ok] 无 $code"
  fi
done
for code in security.W005 security.W008 security.W021; do
  if echo "$PROD_OUT" | grep -q "$code"; then
    echo "[note] ${code}（需 Nginx 强制 HTTPS / HSTS，见报告部署门）"
  fi
done

run "3. HTTPS=1 的 Cookie/HSTS/代理头"
DJANGO_DEBUG=0 DJANGO_HTTPS=1 $PY - <<'PYEOF'
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'stdlib_site.settings')
django.setup()
from django.conf import settings
checks = {
    'SESSION_COOKIE_SECURE': settings.SESSION_COOKIE_SECURE,
    'CSRF_COOKIE_SECURE': settings.CSRF_COOKIE_SECURE,
    'SECURE_HSTS_SECONDS': settings.SECURE_HSTS_SECONDS > 0,
    'SECURE_PROXY_SSL_HEADER': settings.SECURE_PROXY_SSL_HEADER == ('HTTP_X_FORWARDED_PROTO', 'https'),
}
for key, value in checks.items():
    print(f"  {'[ok]' if value else '[!!]'} {key}={settings.__dict__.get(key, value)}")
raise SystemExit(0 if all(checks.values()) else 1)
PYEOF
if [ $? -ne 0 ]; then echo "[!!] HTTPS 安全开关未全部生效"; FAIL=1; fi

run "4. 部署脚本：迁移前备份"
if grep -qi "backup" deploy/deploy.sh; then
  echo "[ok] deploy.sh 含备份步骤"
else
  echo "[!!] deploy/deploy.sh 在 migrate 前没有备份（上线阻塞项）"; FAIL=1
fi

run "5. admin / 登录入口"
if grep -q "path('admin/', admin.site.urls)" stdlib_site/urls.py; then
  echo "[ok] /admin/ 已挂载"
else
  echo "[!!] /admin/ 未挂载"; FAIL=1
fi
if grep -q "LOGIN_URL" stdlib_site/settings.py; then
  echo "[ok] LOGIN_URL 已配置（非 staff 销售也可登录报价）"
else
  echo "[!!] 未配置 LOGIN_URL"; FAIL=1
fi

echo
if [ $FAIL -eq 0 ]; then
  echo "安全配置验收：PASS（其余需在 staging 按 env.example 部署后复验）"
else
  echo "安全配置验收：FAIL（见上方 [!!]）"
fi
exit $FAIL
