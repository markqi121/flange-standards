#!/usr/bin/env python3
"""报价功能 · PDF(Playwright) 环境探测（QA 验收 F）。

    .venv/bin/python scripts/acceptance/playwright_probe.py --json

结论写入验收报告：
- Python 包 playwright 是否安装；
- Chromium 浏览器是否已下载（PLAYWRIGHT_BROWSERS_PATH / 默认缓存）；
- 能否真正渲染一页 A4 PDF（能力验证，不只是 import）。

退出码：0 = 可用；2 = 环境未就绪（记录为部署环境阻塞，不伪造通过）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def probe() -> dict:
    result = {
        'ok': False,
        'python_package': False,
        'browsers_path': os.environ.get('PLAYWRIGHT_BROWSERS_PATH', ''),
        'browser_dirs': [],
        'chromium_launch': False,
        'pdf_rendered': False,
        'pdf_bytes': 0,
        'error': '',
    }
    package_path = None
    try:
        import playwright  # noqa: F401
        result['python_package'] = True
        package_path = Path(playwright.__file__).parent
    except Exception as exc:  # noqa: BLE001
        result['error'] = f'playwright 未安装：{exc!r}'

    candidates = []
    if result['browsers_path']:
        candidates.append(Path(result['browsers_path']))
    home = Path.home()
    candidates.append(home / 'Library' / 'Caches' / 'ms-playwright')
    candidates.append(home / '.cache' / 'ms-playwright')
    for candidate in candidates:
        if candidate.exists():
            result['browser_dirs'] = sorted(
                child.name for child in candidate.iterdir() if child.is_dir()
            )
            break

    if not result['python_package']:
        return result

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
            try:
                result['chromium_launch'] = True
                page = browser.new_page()
                page.set_content(
                    '<html><head><style>@page{size:A4;margin:10mm}</style></head>'
                    '<body><h1>QA PDF probe</h1></body></html>'
                )
                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as handle:
                    target = Path(handle.name)
                page.pdf(path=str(target), format='A4', print_background=True)
                size = target.stat().st_size
                result['pdf_bytes'] = size
                result['pdf_rendered'] = size > 1000
                target.unlink(missing_ok=True)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        result['error'] = f'Chromium 启动/渲染失败：{exc!r}'
    result['ok'] = result['python_package'] and result['chromium_launch'] and result['pdf_rendered']
    return result


def main() -> int:
    result = probe()
    if '--json' in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('PDF(Playwright) 环境探测')
        print(f"  Python 包：{'已安装' if result['python_package'] else '未安装'}")
        print(f"  浏览器目录：{result['browser_dirs'] or '未找到'}")
        print(f"  Chromium 启动：{result['chromium_launch']}")
        print(f"  A4 PDF 渲染：{result['pdf_rendered']}（{result['pdf_bytes']} bytes）")
        if result['error']:
            print(f"  错误：{result['error']}")
        print(f"  结论：{'可用' if result['ok'] else '环境未就绪'}")
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
