"""CSV 导入：材料价格 / 工艺费率 / 客户。

用法::

    python manage.py import_prices --kind materials --file prices.csv
    python manage.py import_prices --kind rates --file rates.csv --dry-run
    python manage.py import_prices --kind customers --file customers.csv --strict
    python manage.py import_prices --kind materials --template -        # 打印模板
    python manage.py import_prices --kind rates --template rates.csv   # 写模板文件

行为：

- 唯一键 ``(material, effective_from)`` / ``code`` / ``code``，用
  ``update_or_create`` 写库，同一个文件重复导入不会产生重复行（幂等）；
- 逐行校验日期/数字/单位/JSON 档位/枚举值，错误带 CSV 行号（含字段名）；
- 默认导入合法行、报告错误行并以非零状态退出；``--strict`` 则任何一行有错
  整批不写；``--dry-run`` 只解析统计、不写库；
- 材料价 ``source`` 用 ``demo`` 时写入演示来源，用于区分演示价与真实采购/市场价。
"""

import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from quote import prices


class Command(BaseCommand):
    help = '从 CSV 导入材料价格 / 工艺费率 / 客户（幂等；错误行逐行报告）。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--kind',
            choices=tuple(sorted(prices.CSV_KINDS)),
            help='数据类型：materials=材料价格，rates=工艺费率，customers=客户',
        )
        parser.add_argument(
            '--file',
            dest='path',
            help='CSV 文件路径；用 - 读取标准输入',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='只解析/校验/统计，不写数据库',
        )
        parser.add_argument(
            '--strict',
            action='store_true',
            help='任何一行有错则整批不写（默认导入合法行并报告错误）',
        )
        parser.add_argument(
            '--encoding',
            default='utf-8-sig',
            help='CSV 编码（默认 utf-8-sig，兼容 Excel 导出的 BOM）',
        )
        parser.add_argument(
            '--template',
            nargs='?',
            const='-',
            default=None,
            metavar='OUT',
            help='输出模板：- 打印到标准输出，或指定文件路径',
        )

    def handle(self, *args, **options):
        kind = options['kind']

        if options['template'] is not None:
            self._write_template(kind, options['template'])
            return

        if not kind:
            raise CommandError(
                '必须指定 --kind materials|rates|customers（或 --template 配合 --kind）'
            )
        if not options['path']:
            raise CommandError('必须指定 --file（- 表示标准输入）')
        if options['strict'] and options['dry_run']:
            # 可以共存：strict 先整批校验，dry-run 反正不写；这里只提示不阻断。
            self.stdout.write(self.style.WARNING('提示：--strict 与 --dry-run 同时使用，只校验不写库。'))

        source = sys.stdin if options['path'] == '-' else options['path']
        try:
            result = prices.import_csv(
                kind,
                source,
                dry_run=options['dry_run'],
                strict=options['strict'],
                encoding=options['encoding'],
            )
        except prices.CSVError as exc:
            raise CommandError(str(exc)) from exc

        self._report(result, dry_run=options['dry_run'], strict=options['strict'])
        if result.errors:
            raise CommandError(
                f'导入完成，但有 {len(result.errors)} 行错误；'
                '错误行未写入，请修正后重试（或用 --strict 整批回滚）。'
            )

    # ------------------------------------------------------------------ helpers
    def _write_template(self, kind, target):
        if not kind:
            raise CommandError('--template 需要同时指定 --kind materials|rates|customers')
        try:
            text = prices.csv_template_text(kind)
        except prices.CSVError as exc:
            raise CommandError(str(exc)) from exc

        if target in ('-', ''):
            self.stdout.write(text.rstrip('\n'))
            return
        path = Path(target)
        if path.parent and not path.parent.exists():
            raise CommandError(f'模板输出目录不存在：{path.parent}')
        path.write_text(text, encoding='utf-8-sig')
        self.stdout.write(self.style.SUCCESS(f'已写入 {prices.CSV_KINDS[kind]["label"]} 模板：{path}'))

    def _report(self, result, *, dry_run: bool, strict: bool):
        label = prices.CSV_KINDS[result.kind]['label']
        if dry_run:
            self.stdout.write(
                f'[dry-run] {label}：共 {result.row_count} 行，'
                f'将新增 {result.created}，将更新 {result.updated}，错误 {result.error_count}'
            )
        elif strict and result.errors:
            self.stdout.write(
                self.style.ERROR(
                    f'{label}：共 {result.row_count} 行，检出 {result.error_count} 行错误，'
                    '已整批回滚（--strict），未写入任何数据'
                )
            )
        else:
            self.stdout.write(
                f'{label}：共 {result.row_count} 行，新增 {result.created}，'
                f'更新 {result.updated}，错误 {result.error_count}'
            )

        for error in result.errors:
            self.stdout.write(self.style.ERROR(f'  {error}'))
