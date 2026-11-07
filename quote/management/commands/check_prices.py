"""生产上线门：检查当前生效的材料价 / 工艺费率是否真实可用。

用法::

    python manage.py check_prices              # 人类可读清单；通过 exit 0，不通过 exit 2
    python manage.py check_prices --json       # 机器可读 JSON（可被 deploy.sh 解析）
    python manage.py check_prices --as-of 2026-11-06

判定（与 ``quote.permissions`` 的演示价格识别一致）：

- 材料价：每个 ``Material(active=True)`` 在报价日之前最近一条 ``MaterialPrice``；
  - 没有记录 → ``missing``（不通过）
  - ``source=demo`` 或备注含“演示” → ``demo``（不通过）
  - 单价 <= 0 → ``invalid``（不通过）
- 工艺费率：``machining/surface/packaging/inspection/freight/mgmt`` 六个 code；
  - 没有生效记录 → ``missing``（不通过）
  - ``source=demo`` 或 label 含“演示” → ``demo``（不通过）

退出码：全部真实可用 exit 0；存在 missing/demo/invalid 任意一项 exit 2。
``deploy.sh`` 可在发布前调用本命令（当前不强制）。

JSON 结构::

    {
      "ok": false,
      "as_of": "2026-11-06",
      "material_prices": [{"material": "A105", "status": "ok", "source": "purchase", ...}],
      "process_rates": [{"code": "freight", "status": "demo", "source": "demo", ...}],
      "summary": {"materials": {...}, "process_rates": {...}, "failures": 1},
      "failures": [ ... ]
    }
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from quote import pricing, services
from quote.models import Material, MaterialPrice
from quote.services import RATE_CODES

STATUS_OK = 'ok'
STATUS_DEMO = 'demo'
STATUS_MISSING = 'missing'
STATUS_INVALID = 'invalid'
FAILING_STATUSES = (STATUS_DEMO, STATUS_MISSING, STATUS_INVALID)

DATE_FORMATS = ('%Y-%m-%d', '%Y/%m/%d')


def _counts(rows: List[dict]) -> dict:
    summary = {STATUS_OK: 0, STATUS_DEMO: 0, STATUS_MISSING: 0, STATUS_INVALID: 0}
    for row in rows:
        summary[row['status']] = summary.get(row['status'], 0) + 1
    return summary


class Command(BaseCommand):
    help = '检查当前生效的材料价/工艺费率是否存在 demo 或缺失（生产上线门；不通过 exit 2）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--json', action='store_true', dest='json',
            help='输出 JSON（供 deploy.sh/CI 解析）',
        )
        parser.add_argument(
            '--as-of', dest='as_of', default=None,
            help='检查日期 YYYY-MM-DD，默认今天（本地时区）',
        )

    # ------------------------------------------------------------------ 数据
    def _parse_as_of(self, value) -> 'datetime.date':
        if not value:
            return timezone.localdate()
        text = str(value).strip()
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        raise CommandError(f'--as-of 日期格式应为 YYYY-MM-DD：{value!r}')

    def _material_rows(self, as_of) -> List[dict]:
        rows = []
        materials = Material.objects.filter(active=True).order_by('sort_order', 'code')
        for material in materials:
            price = (
                MaterialPrice.objects.filter(material=material, effective_from__lte=as_of)
                .order_by('-effective_from', '-id')
                .first()
            )
            if price is None:
                status = STATUS_MISSING
                message = f'{as_of} 之前没有生效材料价'
            elif services.is_demo_material_price(price):
                status = STATUS_DEMO
                message = '演示价格（source=demo 或备注含“演示”）'
            elif price.price_per_kg is None or price.price_per_kg <= 0:
                status = STATUS_INVALID
                message = '单价必须大于 0'
            else:
                status = STATUS_OK
                message = ''
            rows.append({
                'material': material.code,
                'label': material.label,
                'status': status,
                'message': message,
                'source': getattr(price, 'source', None) if price else None,
                'price_per_kg': float(price.price_per_kg) if price else None,
                'currency': getattr(price, 'currency', None) if price else None,
                'effective_from': price.effective_from.isoformat() if price else None,
            })
        return rows

    def _rate_rows(self, as_of) -> List[dict]:
        rates = pricing.all_rates(as_of)
        rows = []
        for code in RATE_CODES:
            rate = rates.get(code)
            if rate is None:
                status = STATUS_MISSING
                message = f'{as_of} 之前没有生效费率'
            elif services.is_demo_process_rate(rate):
                status = STATUS_DEMO
                message = '演示费率（source=demo 或 label 含“演示”）'
            else:
                status = STATUS_OK
                message = ''
            rows.append({
                'code': code,
                'label': getattr(rate, 'label', None) if rate else None,
                'status': status,
                'message': message,
                'source': getattr(rate, 'source', None) if rate else None,
                'rate': float(rate.rate) if rate else None,
                'unit': getattr(rate, 'unit', None) if rate else None,
                'effective_from': rate.effective_from.isoformat() if rate else None,
            })
        return rows

    # ------------------------------------------------------------------ 输出
    def _print_human(self, payload: dict, as_of) -> None:
        self.stdout.write(f'价格检查（as_of {as_of.isoformat()}）')
        self.stdout.write('材料价（active）：')
        for row in payload['material_prices']:
            price = '—' if row['price_per_kg'] is None else f"{row['price_per_kg']:.2f} 元/kg"
            extra = f" · 生效 {row['effective_from']}" if row['effective_from'] else ''
            note = f" · {row['message']}" if row['message'] else ''
            self.stdout.write(
                f"  [{self._tag(row['status'])}] {row['material']} {price}{extra}{note}"
            )
        self.stdout.write('工艺费率：')
        for row in payload['process_rates']:
            rate = '—' if row['rate'] is None else f"{row['rate']:.2f}"
            extra = f" · 生效 {row['effective_from']}" if row['effective_from'] else ''
            note = f" · {row['message']}" if row['message'] else ''
            self.stdout.write(
                f"  [{self._tag(row['status'])}] {row['code']} {rate}{extra}{note}"
            )
        summary = payload['summary']
        if payload['ok']:
            self.stdout.write(self.style.SUCCESS(
                f"结论：通过（材料价 {summary['materials'][STATUS_OK]} 项、"
                f"费率 {summary['process_rates'][STATUS_OK]} 项均为真实来源）"
            ))
        else:
            self.stdout.write(self.style.ERROR(
                f"结论：不通过（{summary['failures']} 项 missing/demo/invalid）"
            ))

    @staticmethod
    def _tag(status: str) -> str:
        return {
            STATUS_OK: 'OK',
            STATUS_DEMO: '演示',
            STATUS_MISSING: '缺失',
            STATUS_INVALID: '非法',
        }.get(status, status)

    # ------------------------------------------------------------------ 入口
    def handle(self, *args, **options):
        as_of = self._parse_as_of(options.get('as_of'))
        material_rows = self._material_rows(as_of)
        rate_rows = self._rate_rows(as_of)
        failures = [
            {'kind': 'material_price', **row}
            for row in material_rows if row['status'] in FAILING_STATUSES
        ] + [
            {'kind': 'process_rate', **row}
            for row in rate_rows if row['status'] in FAILING_STATUSES
        ]
        payload = {
            'ok': not failures,
            'as_of': as_of.isoformat(),
            'material_prices': material_rows,
            'process_rates': rate_rows,
            'summary': {
                'materials': _counts(material_rows),
                'process_rates': _counts(rate_rows),
                'failures': len(failures),
            },
            'failures': failures,
        }

        if options.get('json'):
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            self._print_human(payload, as_of)

        if failures:
            kinds = ', '.join(
                f"{row['kind']}:{row.get('material') or row.get('code')}"
                for row in failures[:8]
            )
            more = '' if len(failures) <= 8 else f' 等 {len(failures)} 项'
            raise CommandError(
                f'价格检查未通过（{len(failures)} 项）：{kinds}{more}', returncode=2
            )
        return None
