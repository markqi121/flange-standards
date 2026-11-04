"""
法兰标准库 · Django 配置

本地开发直接 `python manage.py runserver`；
生产环境用环境变量覆盖 SECRET_KEY / DEBUG / ALLOWED_HOSTS。
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-dev-only-change-me-flange-standards-library',
)

DEBUG = os.environ.get('DJANGO_DEBUG', '1') == '1'

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',')
    if h.strip()
]

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',')
    if o.strip()
]

# 生产环境在 Nginx 后面时打开：DJANGO_HTTPS=1
# 挂载在子路径时（例如 https://ax-forge.com/std/）设置 DJANGO_SCRIPT_NAME=/std
SCRIPT_NAME = os.environ.get('DJANGO_SCRIPT_NAME', '').rstrip('/')
if SCRIPT_NAME:
    FORCE_SCRIPT_NAME = SCRIPT_NAME

HTTPS = os.environ.get('DJANGO_HTTPS', '0') == '1'
if HTTPS:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'catalog',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'catalog.middleware.PublicCacheMiddleware',
]

ROOT_URLCONF = 'stdlib_site.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'catalog.context.site_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'stdlib_site.wsgi.application'

# 默认 SQLite；设了 DJANGO_DB_ENGINE 就切到 PostgreSQL（见 docs/部署指南.md）
if os.environ.get('DJANGO_DB_ENGINE'):
    DATABASES = {
        'default': {
            'ENGINE': os.environ['DJANGO_DB_ENGINE'],
            'NAME': os.environ.get('DJANGO_DB_NAME', 'flange_std'),
            'USER': os.environ.get('DJANGO_DB_USER', ''),
            'PASSWORD': os.environ.get('DJANGO_DB_PASSWORD', ''),
            'HOST': os.environ.get('DJANGO_DB_HOST', ''),
            'PORT': os.environ.get('DJANGO_DB_PORT', '5432'),
            'CONN_MAX_AGE': 60,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': Path(os.environ.get('DJANGO_DB_PATH', BASE_DIR / 'db.sqlite3')),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# 中文界面 + 中国时区（打印页的生成时间按此显示）
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

STATIC_URL = f'{SCRIPT_NAME}/static/' if SCRIPT_NAME else 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# 数据包位置：flange-standards-data/ 与项目同级
DATA_DIR = Path(os.environ.get('FLANGE_DATA_DIR', BASE_DIR / 'flange-standards-data'))

# 站点标识，打印页与页脚使用
SITE_NAME = '法兰标准库'
SITE_TAGLINE = '法兰尺寸 · 压力等级 · 打印规格卡'
SITE_URL = os.environ.get('SITE_URL', 'http://127.0.0.1:8000').rstrip('/')

# PDF 生成：Playwright 浏览器路径 / 内部渲染地址（生产环境在 env 中覆盖）
PDF_RENDER_BASE_URL = os.environ.get('PDF_RENDER_BASE_URL', 'https://127.0.0.1/std').rstrip('/')
PDF_CACHE_DIR = os.environ.get('PDF_CACHE_DIR', '/var/lib/flange-std/pdf')
