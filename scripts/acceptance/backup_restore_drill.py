#!/usr/bin/env python3
"""报价功能 · 备份/恢复演练（QA 验收 I）。

用途（T4 验收 / 上线前演练 / 迁移前备份）：

    .venv/bin/python scripts/acceptance/backup_restore_drill.py \
        --source db.sqlite3 --backup-dir /tmp/quote-backup-$(date +%F) \
        --pdf-dir /var/lib/flange-std/pdf --json

做四件事，任何一项失败都会以非 0 退出并把证据写进 JSON：

1. 用 sqlite3 在线备份 API 备份数据库（不是 cp，避免半写文件）；
2. 校验备份 `PRAGMA integrity_check`，并对比关键表行数；
3. 把备份"恢复"到临时文件，再校验完整性与行数——证明备份可用；
4. 备份 PDF 缓存目录（报价单 PDF），逐个比对 sha256。

迁移前备份：在执行 ``manage.py migrate`` 之前先跑本脚本（``--label pre-migrate``）。
回滚：恢复数据库 + PDF 目录，代码回退到上一版本，再执行 migrate 到旧迁移点。
本脚本只读源库、只写备份目录，不改业务数据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# 关键业务表：验收与恢复后逐表核对行数
KEY_TABLES = (
    'catalog_standard',
    'catalog_flangetype',
    'catalog_rating',
    'catalog_dimensionrow',
    'quote_material',
    'quote_materialprice',
    'quote_processrate',
    'quote_customer',
    'quote_costsnapshot',
    'quote_quotation',
    'quote_quotationitem',
    'quote_approvallog',
    'auth_user',
    'django_migrations',
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        connection.close()


def _table_counts(path: Path) -> dict:
    connection = sqlite3.connect(path)
    try:
        existing = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {}
        for table in KEY_TABLES:
            if table in existing:
                counts[table] = connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
        return counts
    finally:
        connection.close()


def _prune(backup_dir: Path, keep: int) -> list:
    backups = sorted(backup_dir.glob('db-*.sqlite3'))
    removed = []
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
        removed.append(str(old))
    return removed


def _backup_pdf(pdf_dir: Path, backup_dir: Path) -> dict:
    result = {
        'source_dir': str(pdf_dir) if pdf_dir else None,
        'backup_dir': str(backup_dir / 'pdf'),
        'files_backed_up': 0,
        'files_verified': 0,
        'checksum_match': False,
        'skipped': True,
        'reason': '',
        'files': [],
    }
    if pdf_dir is None or not pdf_dir.exists():
        result['reason'] = '未提供/不存在 PDF 目录（Playwright 未部署时属正常）'
        return result
    target = backup_dir / 'pdf'
    target.mkdir(parents=True, exist_ok=True)
    files = sorted(path for path in pdf_dir.rglob('*') if path.is_file())
    result['skipped'] = False
    result['files_backed_up'] = len(files)
    matched = 0
    for source_file in files:
        relative = source_file.relative_to(pdf_dir)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
        source_hash = _sha256(source_file)
        destination_hash = _sha256(destination)
        entry = {
            'file': str(relative),
            'bytes': source_file.stat().st_size,
            'sha256': source_hash,
            'restored_match': source_hash == destination_hash,
        }
        if entry['restored_match']:
            matched += 1
        result['files'].append(entry)
    result['files_verified'] = matched
    result['checksum_match'] = matched == len(files)
    return result


def run(source: Path, backup_dir: Path, pdf_dir: Path | None,
        label: str | None = None, keep: int = 14) -> dict:
    checks = []

    def check(name: str, ok: bool, detail: str = '') -> None:
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})

    check('source_exists', source.exists(), str(source))
    if not source.exists():
        return {'ok': False, 'checks': checks, 'error': f'数据库不存在：{source}'}

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
    suffix = f'-{label}' if label else ''
    backup_path = backup_dir / f'db-{stamp}{suffix}.sqlite3'

    # 1) 在线备份
    with sqlite3.connect(source) as source_conn, sqlite3.connect(backup_path) as dest_conn:
        source_conn.backup(dest_conn)
    check('backup_created', backup_path.exists() and backup_path.stat().st_size > 0,
          f'{backup_path} ({backup_path.stat().st_size if backup_path.exists() else 0} bytes)')

    source_integrity = _integrity(source)
    backup_integrity = _integrity(backup_path)
    check('source_integrity', source_integrity == 'ok', source_integrity)
    check('integrity_check', backup_integrity == 'ok', backup_integrity)

    source_counts = _table_counts(source)
    backup_counts = _table_counts(backup_path)
    check('backup_counts_match', source_counts == backup_counts,
          json.dumps({'source': source_counts, 'backup': backup_counts}, ensure_ascii=False))

    # 2) 恢复演练：把备份复制到临时文件再打开，证明备份可恢复
    restore_dir = backup_dir / 'restore'
    restore_dir.mkdir(parents=True, exist_ok=True)
    restore_path = restore_dir / f'restored-{stamp}{suffix}.sqlite3'
    shutil.copy2(backup_path, restore_path)
    restore_integrity = _integrity(restore_path)
    restore_counts = _table_counts(restore_path)
    check('restore_integrity', restore_integrity == 'ok', restore_integrity)
    check('restore_counts_match', restore_counts == source_counts,
          json.dumps({'source': source_counts, 'restored': restore_counts}, ensure_ascii=False))

    # 3) PDF 缓存备份
    pdf_result = _backup_pdf(pdf_dir, backup_dir)
    if pdf_result['skipped']:
        check('pdf_backup', True, 'skipped：' + pdf_result['reason'])
    else:
        check('pdf_backup',
              pdf_result['checksum_match'] and pdf_result['files_backed_up'] > 0,
              f"{pdf_result['files_backed_up']} 个文件，校验 {pdf_result['files_verified']} 个")

    # 4) 保留策略
    removed = _prune(backup_dir, keep)
    check('retention', len(removed) <= max(keep, 0),
          f'保留最近 {keep} 份，本次清理 {len(removed)} 份')

    critical = [item for item in checks if item['name'] != 'pdf_backup']
    ok = all(item['ok'] for item in critical) and (
        pdf_result['skipped'] or pdf_result['checksum_match']
    )
    return {
        'ok': bool(ok),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'label': label or '',
        'source': str(source),
        'database': {
            'size_bytes': source.stat().st_size,
            'integrity': source_integrity,
            'backup_exists': backup_path.exists(),
            'backup_path': str(backup_path),
            'backup_size_bytes': backup_path.stat().st_size if backup_path.exists() else 0,
            'counts': source_counts,
            'restore_path': str(restore_path),
            'restore_integrity': restore_integrity,
            'restore_counts': restore_counts,
            'retention_keep': keep,
            'removed': removed,
        },
        'pdf': pdf_result,
        'checks': checks,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='报价功能 · 备份/恢复演练')
    parser.add_argument('--source', default=None, help='源 SQLite 数据库路径')
    parser.add_argument('--backup-dir', default=None, help='备份目录')
    parser.add_argument('--pdf-dir', default=None, help='PDF 缓存目录（可选）')
    parser.add_argument('--label', default=None, help='备份标签，如 pre-migrate')
    parser.add_argument('--keep', type=int, default=14, help='保留份数（默认 14）')
    parser.add_argument('--json', action='store_true', help='只输出 JSON（供自动化消费）')
    args = parser.parse_args(argv)

    import os

    source = Path(args.source or os.environ.get('DJANGO_DB_PATH', 'db.sqlite3')).resolve()
    backup_dir = Path(
        args.backup_dir or (Path(tempfile.gettempdir()) / 'quote-acceptance-backups')
    ).resolve()
    pdf_dir = None
    if args.pdf_dir:
        pdf_dir = Path(args.pdf_dir).resolve()
    elif os.environ.get('PDF_CACHE_DIR'):
        candidate = Path(os.environ['PDF_CACHE_DIR']).resolve()
        pdf_dir = candidate if candidate.exists() else None

    result = run(source, backup_dir, pdf_dir, label=args.label, keep=args.keep)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        summary = 'PASS' if result['ok'] else 'FAIL'
        print(f'备份/恢复演练：{summary}')
        print(f'  源库：{result.get("source")}')
        if result.get('database'):
            db = result['database']
            print(f'  备份：{db["backup_path"]} ({db["backup_size_bytes"]} bytes)')
            print(f'  完整性：{db["integrity"]} / 恢复校验：{db["restore_integrity"]}')
            print(f'  关键表行数：{db["counts"]}')
        for item in result.get('checks', []):
            print(f"  [{'ok' if item['ok'] else '!!'}] {item['name']}: {item['detail']}")
        if result.get('error'):
            print(f'  error: {result["error"]}')
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
