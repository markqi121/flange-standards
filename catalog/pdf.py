"""服务端 A4 PDF 生成。

手机（尤其 iPhone Safari）不支持 window.print()，所以提供「下载 PDF」：
用 Playwright 驱动 Chromium 渲染内部打印页，生成与桌面打印一致的 A4 PDF。

依赖：
- pip install playwright
- playwright install chromium（浏览器放 PLAYWRIGHT_BROWSERS_PATH）
- 系统依赖：playwright install-deps chromium

渲染地址默认走本机 Nginx 的 /std/ 打印页，这样 CSS/静态文件与线上完全一致；
可用 PDF_RENDER_BASE_URL 覆盖。
"""

import fcntl
import logging
import os
import pathlib
import tempfile

logger = logging.getLogger(__name__)

CACHE_DIR = pathlib.Path(os.environ.get('PDF_CACHE_DIR', '/var/lib/flange-std/pdf'))
RENDER_BASE = os.environ.get('PDF_RENDER_BASE_URL', 'https://127.0.0.1/std').rstrip('/')
LAYOUT_VERSION = os.environ.get('PDF_LAYOUT_VERSION', '1')


def _cached_path(row):
    return CACHE_DIR / f'spec-{row.pk}-v{LAYOUT_VERSION}.pdf'


def _is_fresh(path, row):
    try:
        return path.exists() and path.stat().st_size > 1000 and path.stat().st_mtime >= row.updated_at.timestamp()
    except OSError:
        return False


def _generate(target, row):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - 依赖缺失时的降级
        raise RuntimeError('Playwright 未安装，无法生成 PDF') from exc

    url = f'{RENDER_BASE}/spec/{row.pk}/print/'
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f'spec-{row.pk}-', suffix='.pdf', dir=target.parent)
        os.close(fd)
        tmp_path = pathlib.Path(tmp_name)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
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

        if tmp_path.stat().st_size < 1000:
            raise RuntimeError('生成的 PDF 过小，可能渲染失败')
        os.replace(tmp_path, target)
        logger.info('PDF generated: %s (%s bytes)', target, target.stat().st_size)
        return target
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def render_spec_pdf(row, force=False):
    """返回 PDF 文件路径；成功后才移动为最终文件，避免半成品被读到。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = _cached_path(row)
    if not force and _is_fresh(target, row):
        return target

    lock_path = CACHE_DIR / f'.spec-{row.pk}.lock'
    with open(lock_path, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not force and _is_fresh(target, row):
            return target

        # 全局串行：避免多个不同规格同时拉起 Chromium 把内存打满
        with open(CACHE_DIR / '.render.lock', 'w') as global_lock:
            fcntl.flock(global_lock, fcntl.LOCK_EX)
            if not force and _is_fresh(target, row):
                return target
            return _generate(target, row)
