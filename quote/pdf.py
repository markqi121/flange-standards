"""法兰报价 · A4 报价单 PDF（服务端渲染）。

复用站点既有方案：Playwright 驱动 Chromium 打开内部打印页
（`/quote/<number>/print/`）→ 生成与浏览器打印一致的 A4 PDF。

手机（尤其 iPhone Safari）不能可靠地 window.print()，所以报价单详情/打印页
都提供「下载 PDF」入口；生成失败时视图会降级为 503 提示页，引导使用浏览器打印。

依赖（与 catalog/pdf.py 一致）：
- pip install playwright
- playwright install chromium
- 可用 PDF_CACHE_DIR / PDF_RENDER_BASE_URL 覆盖缓存目录与内部渲染地址

兜底：
- Playwright 不可用但配置了 PDF_CHROME_BIN（env 或 settings）时，用 Chrome/Chromium
  CLI `--headless --print-to-pdf` 渲染同一张带 pdf_token 的打印页；
- 两者都不可用抛 RuntimeError，由 views 返回 503 并引导浏览器打印。
"""

import logging
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote

from django.conf import settings
from django.core import signing

logger = logging.getLogger(__name__)

LAYOUT_VERSION = os.environ.get('PDF_LAYOUT_VERSION', '1')

# 打印页默认要求登录；PDF 渲染器是无会话的 Chromium，因此用短时效签名令牌
# 只授权“这一张报价单的打印页”，避免为了 PDF 把 /quote/ 打印页改成公开。
PDF_TOKEN_SALT = 'quote-print-pdf'
PDF_TOKEN_MAX_AGE = 600


def print_page_token(number, demo=False):
    """生成打印页短时效令牌（PDF 渲染专用）。"""
    return signing.dumps({'n': str(number), 'd': bool(demo)}, salt=PDF_TOKEN_SALT)


def verify_print_page_token(token, number, demo=False):
    if not token:
        return False
    try:
        payload = signing.loads(token, salt=PDF_TOKEN_SALT, max_age=PDF_TOKEN_MAX_AGE)
    except signing.BadSignature:
        return False
    return (
        isinstance(payload, dict)
        and str(payload.get('n')) == str(number)
        and bool(payload.get('d')) == bool(demo)
    )


def _safe(number):
    return re.sub(r'[^0-9A-Za-z._-]', '_', number or 'quote')


def _cache_dir():
    """缓存目录；生产走 PDF_CACHE_DIR，本地不可写时退回临时目录。"""
    base = pathlib.Path(getattr(settings, 'PDF_CACHE_DIR', tempfile.gettempdir()))
    target = base / 'quote'
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        fallback = pathlib.Path(tempfile.gettempdir()) / 'flange-quote-pdf'
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _target_path(number, demo=False):
    suffix = '-demo' if demo else ''
    return _cache_dir() / f'quote-{_safe(number)}{suffix}-v{LAYOUT_VERSION}.pdf'


def _is_fresh(path, max_age_seconds=300):
    try:
        import time

        return path.exists() and path.stat().st_size > 1000 and (time.time() - path.stat().st_mtime) < max_age_seconds
    except OSError:
        return False


def _render_base_url():
    return (getattr(settings, 'PDF_RENDER_BASE_URL', 'https://127.0.0.1/std') or '').rstrip('/')


def _chrome_binary():
    """Chrome/Chromium CLI 路径：优先 env PDF_CHROME_BIN，其次 settings.PDF_CHROME_BIN。"""
    candidates = [
        os.environ.get('PDF_CHROME_BIN') or '',
        getattr(settings, 'PDF_CHROME_BIN', '') or '',
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = pathlib.Path(str(candidate)).expanduser()
        if path.exists() and path.is_file():
            return str(path)
        logger.warning('PDF_CHROME_BIN 指向的 Chrome 不存在：%s', candidate)
    return ''


def _render_with_playwright(url, tmp_path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            context = browser.new_context(
                ignore_https_errors=True,
                viewport={'width': 1240, 'height': 1754},
            )
            page = context.new_page()
            page.emulate_media(media='print')
            page.goto(url, wait_until='networkidle', timeout=30000)
            page.pdf(
                path=str(tmp_path),
                format='A4',
                print_background=True,
                prefer_css_page_size=True,
            )
        finally:
            browser.close()


def _render_with_chrome(chrome_bin, url, tmp_path):
    """Playwright 不可用时的生产兜底：用 Chrome/Chromium CLI 打印页面为 PDF。"""
    command = [
        chrome_bin,
        '--headless',
        '--no-sandbox',
        '--disable-gpu',
        '--disable-dev-shm-usage',
        '--no-pdf-header-footer',
        '--run-all-compositor-stages-before-draw',
        '--virtual-time-budget=3000',
        f'--print-to-pdf={tmp_path}',
        url,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f'Chrome CLI 不存在：{chrome_bin}') from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Chrome CLI 渲染 PDF 超时') from exc
    if result.returncode != 0:
        stderr = (result.stderr or b'').decode('utf-8', 'replace')[-600:]
        raise RuntimeError(f'Chrome CLI 生成 PDF 失败（退出码 {result.returncode}）：{stderr}')


def render_quote_pdf(number, demo=False, force=False):
    """渲染 `/quote/<number>/print/` 为 A4 PDF，返回缓存文件路径。

    渲染顺序：
    1. Playwright（与 catalog/pdf.py 一致，生产首选）；
    2. Playwright 不可用时，如果 PDF_CHROME_BIN / settings.PDF_CHROME_BIN 指向
       Chrome/Chromium，则用 Chrome CLI `--headless --print-to-pdf` 兜底；
    3. 两者都不可用 → RuntimeError，由 views.quote_pdf 转 503 降级页。
    """
    target = _target_path(number, demo=demo)
    if not force and _is_fresh(target):
        return target

    token = print_page_token(number, demo=demo)
    url = f'{_render_base_url()}/quote/{quote(str(number))}/print/?pdf_token={quote(token)}'
    if demo:
        url += '&demo=1'

    fd, tmp_name = tempfile.mkstemp(prefix=f'quote-{_safe(number)}-', suffix='.pdf', dir=str(target.parent))
    os.close(fd)
    tmp_path = pathlib.Path(tmp_name)
    try:
        try:
            _render_with_playwright(url, tmp_path)
        except Exception as playwright_exc:  # 未安装 playwright / 浏览器，进入 CLI 兜底
            chrome_bin = _chrome_binary()
            if not chrome_bin:
                raise RuntimeError(
                    'Playwright 未安装，且未配置 PDF_CHROME_BIN 指向的 Chrome/Chromium；'
                    '请安装 PDF 依赖或使用浏览器打印。'
                ) from playwright_exc
            logger.info('Playwright 不可用，改用 Chrome CLI 生成报价单 PDF：%s', chrome_bin)
            _render_with_chrome(chrome_bin, url, tmp_path)

        if tmp_path.stat().st_size < 1000:
            raise RuntimeError('生成的 PDF 过小，可能渲染失败')
        os.replace(str(tmp_path), str(target))
        logger.info('报价单 PDF 已生成：%s（%s bytes）', target, target.stat().st_size)
        return target
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
