#!/usr/bin/env python3
"""SQLite 每日备份：保留最近 14 份。由 cron 以 deploy 用户执行。"""

import datetime
import os
import pathlib
import sqlite3

SOURCE = os.environ.get('DJANGO_DB_PATH', '/var/lib/flange-std/db.sqlite3')
BACKUP_DIR = pathlib.Path(os.environ.get('BACKUP_DIR', '/var/lib/flange-std/backups'))
KEEP = 14


def main():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f'db-{datetime.date.today():%Y-%m-%d}.sqlite3'
    with sqlite3.connect(SOURCE) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    backups = sorted(BACKUP_DIR.glob('db-*.sqlite3'))
    for old in backups[:-KEEP]:
        old.unlink()
    print(f'backup ok: {target} ({target.stat().st_size} bytes), keep {min(len(backups), KEEP)}')


if __name__ == '__main__':
    main()
