#!/usr/bin/env python3
"""报价功能 · 性能/并发冒烟（QA 验收 H）。

对开发库的**临时副本**运行（不写项目 db.sqlite3）：

    .venv/bin/python scripts/acceptance/perf_smoke.py --json

覆盖 DoD §2.4：
- 工作台首屏 <1 s（数据装配部分；模板渲染在 tests_acceptance 里测）；
- 单次计算 <50 ms；
- 1,348 条全量选项/重量/计算冒烟；
- 10 并发读、10 并发写（SQLite 锁风险实测，超限建议切 PostgreSQL）；
- 记录 SQLite journal_mode（是否 WAL）与库大小。

输出 JSON 供报告/CI 消费；读并发失败或有写锁错误时退出码 1。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path


def _copy_database(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as dest_conn:
        source_conn.backup(dest_conn)


def _journal_mode(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute('PRAGMA journal_mode').fetchone()[0]
    finally:
        connection.close()


def _stats(samples):
    ordered = sorted(samples)
    count = len(ordered)
    return {
        'count': count,
        'min_ms': round(min(ordered) * 1000, 3),
        'median_ms': round(ordered[count // 2] * 1000, 3),
        'p95_ms': round(ordered[min(count - 1, int(count * 0.95))] * 1000, 3),
        'max_ms': round(max(ordered) * 1000, 3),
        'mean_ms': round(sum(ordered) / count * 1000, 3),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='报价功能性能/并发冒烟')
    parser.add_argument('--source', default='db.sqlite3', help='源 SQLite 库（只读复制）')
    parser.add_argument('--threads', type=int, default=10)
    parser.add_argument('--reads-per-thread', type=int, default=10)
    parser.add_argument('--writes-per-thread', type=int, default=3)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    if not source.exists():
        print(f'源数据库不存在：{source}', file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix='quote-perf-'))
    copy = workdir / 'db.sqlite3'
    _copy_database(source, copy)

    os.environ['DJANGO_DB_PATH'] = str(copy)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'stdlib_site.settings')
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    import django

    django.setup()

    from django.contrib.auth import get_user_model
    from django.db import connections
    from django.test import RequestFactory

    from catalog.models import DimensionRow
    from quote import services, views, weight as weight_module
    from quote.models import Material

    User = get_user_model()
    result = {
        'ok': False,
        'source': str(source),
        'workdir': str(workdir),
        'journal_mode': _journal_mode(copy),
        'db_size_bytes': copy.stat().st_size,
        'threads': args.threads,
        'metrics': {},
        'concurrency': {},
        'errors': [],
    }

    rows = list(
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating').order_by('pk')
    )
    materials = {material.code: material for material in Material.objects.all()}
    standard_material = {
        'asme-b16-5': 'A105', 'asme-b16-47-a': 'A105', 'asme-b16-47-b': 'A105',
        'en-1092-1': 'P250GH', 'jis-b2220': 'SS400',
    }
    payloads = []
    for row in rows:
        payloads.append({
            'std': row.standard_id,
            'type': row.flange_type_id,
            'size': row.size,
            'rating': row.rating.code,
            'material': standard_material.get(row.standard_id, 'A105'),
            'qty': 10,
            'design_temp_c': 38,
            'design_pressure_bar': 16,
            'surface': 'none',
            'packaging': 'wood',
            'inspections': [],
            'on_date': '2026-01-15',
            'valid_until': '2026-01-30',
        })
    result['metrics']['catalog_rows'] = len(rows)

    # 1) 全量选项
    started = time.perf_counter()
    options = services.catalog_options()
    result['metrics']['catalog_options_s'] = round(time.perf_counter() - started, 4)
    result['metrics']['catalog_options_sizes'] = sum(
        len(sizes) for std in options['catalog'].values()
        for ratings in std.values() for sizes in ratings.values()
    )

    # 2) 全量重量
    started = time.perf_counter()
    weight_errors = 0
    for row in rows:
        try:
            weight_module.resolve_weight(row, materials[standard_material.get(row.standard_id, 'A105')])
        except Exception:  # noqa: BLE001
            weight_errors += 1
    result['metrics']['all_weight_s'] = round(time.perf_counter() - started, 4)
    result['metrics']['weight_errors'] = weight_errors

    # 3) 全量计算
    started = time.perf_counter()
    calc_errors = 0
    for payload in payloads:
        try:
            services.calculate(payload)
        except Exception as exc:  # noqa: BLE001
            calc_errors += 1
            if len(result['errors']) < 10:
                result['errors'].append(f'calculate: {exc!r}')
    result['metrics']['all_calculate_s'] = round(time.perf_counter() - started, 4)
    result['metrics']['calculate_errors'] = calc_errors

    # 4) 单次计算
    services.calculate(payloads[0])
    samples = []
    for _ in range(30):
        started = time.perf_counter()
        services.calculate(payloads[0])
        samples.append(time.perf_counter() - started)
    result['metrics']['single_calculate'] = _stats(samples)

    # 5) 工作台数据装配（首屏重量级部分；模板渲染见 tests_acceptance）
    user = User.objects.first()
    factory = RequestFactory()
    request = factory.get('/quote/')
    request.user = user
    views._workbench_bootstrap(request)  # 预热
    samples = []
    for _ in range(5):
        request = factory.get('/quote/')
        request.user = user
        started = time.perf_counter()
        bootstrap, _standards, _types = views._workbench_bootstrap(request)
        samples.append(time.perf_counter() - started)
    result['metrics']['workbench_bootstrap'] = _stats(samples)
    result['metrics']['workbench_rows'] = len(bootstrap['rows'])

    def read_worker(count):
        from django.db import connections as thread_connections
        errors = []
        try:
            for _ in range(count):
                try:
                    services.calculate(payloads[0])
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))
        finally:
            thread_connections.close_all()
        return errors

    def write_worker(count):
        from django.db import connections as thread_connections
        errors = []
        numbers = []
        try:
            for _ in range(count):
                try:
                    quote = services.create_quote(payloads[0], user)
                    numbers.append(quote.number)
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))
        finally:
            thread_connections.close_all()
        return errors, numbers

    # 6) 并发读
    started = time.perf_counter()
    read_errors = []
    threads = [
        threading.Thread(target=lambda: read_errors.extend(read_worker(args.reads_per_thread)))
        for _ in range(args.threads)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    result['concurrency']['read'] = {
        'threads': args.threads,
        'reads_per_thread': args.reads_per_thread,
        'seconds': round(time.perf_counter() - started, 3),
        'errors': read_errors[:10],
        'error_count': len(read_errors),
    }
    connections.close_all()

    # 7) 并发写（报价单创建，测 SQLite 锁）
    started = time.perf_counter()
    write_errors = []
    numbers = []

    def run_write():
        errors, created = write_worker(args.writes_per_thread)
        write_errors.extend(errors)
        numbers.extend(created)

    threads = [threading.Thread(target=run_write) for _ in range(args.threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    result['concurrency']['write'] = {
        'threads': args.threads,
        'writes_per_thread': args.writes_per_thread,
        'seconds': round(time.perf_counter() - started, 3),
        'created': len(numbers),
        'unique_numbers': len(set(numbers)),
        'errors': write_errors[:10],
        'error_count': len(write_errors),
    }
    connections.close_all()

    result['ok'] = (
        weight_errors == 0
        and calc_errors == 0
        and not read_errors
        and not write_errors
    )
    result['verdict'] = {
        'workbench_under_1s': result['metrics']['workbench_bootstrap']['max_ms'] < 1000,
        'single_calc_under_50ms': result['metrics']['single_calculate']['median_ms'] < 50,
        'sqlite_concurrent_write_ok': not write_errors,
        'recommendation': (
            '当前 SQLite 并发写无锁错误' if not write_errors
            else 'SQLite 并发写存在锁错误；生产多人同时录单请切 PostgreSQL（见部署指南 §4）'
        ),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('报价功能性能/并发冒烟')
        print(f"  规格行数：{result['metrics']['catalog_rows']}")
        print(f"  全量选项：{result['metrics']['catalog_options_s']}s")
        print(f"  全量重量：{result['metrics']['all_weight_s']}s（错误 {weight_errors}）")
        print(f"  全量计算：{result['metrics']['all_calculate_s']}s（错误 {calc_errors}）")
        print(f"  单次计算：{result['metrics']['single_calculate']}")
        print(f"  工作台数据装配：{result['metrics']['workbench_bootstrap']}")
        print(f"  并发读：{result['concurrency']['read']}")
        print(f"  并发写：{result['concurrency']['write']}")
        print(f"  SQLite journal_mode：{result['journal_mode']}")
        print(f"  结论：{'PASS' if result['ok'] else 'FAIL'}")
        for error in result['errors'][:5]:
            print(f'  error: {error}')

    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
