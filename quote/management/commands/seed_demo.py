"""演示种子数据：23 个材质 + 材料价、工艺费率、包装/检测/运费、客户 A/B/C。

⚠️ 这些全部是**演示数据**，禁止当作真实价格上线：

- 材料单价与 ``docs/法兰报价系统设计方案.html`` 第 7 节“材质主数据（节选）”的原型
  值一致（23 个牌号，元/kg 含税演示口径）；``MaterialPrice.source`` 统一写
  ``'demo'``，``note`` 明确“演示价，禁止用于生产报价”；
- 工艺费率与 ``quote/costing.py`` 的演示默认值 ``QUOTE_DEMO_RATES`` 数值一致，
  只用于界面联调；``ProcessRate.source`` 统一写 ``'demo'``（T5 已加字段与迁移），
  label 同时保留“（演示）”字样，界面/导出都能看出是示例；
- 客户 ``demo-a/b/c`` 对应 A/B/C 三档、折扣 8/4/0%，只用于演示与测试；
- 重复执行幂等：材质只补缺不覆盖；演示价/费率/客户按唯一键更新。若库里已存在
  **非演示** 的同键价格/费率，种子会跳过并提示，不会用演示值覆盖真实数据。
- 工艺费率的 ``machining`` 档位键用数字 DN 上限（``{"50": 12, ...}``），因为
  ``services._resolve_rates`` 会把它转成 ``costing.machining_base`` 的累计档位；
  CSV 导入允许契约示例写法 ``<=50`` 并自动规范化为 ``"50"``。

用法::

    python manage.py seed_demo                 # DEBUG=1 直接写入
    python manage.py seed_demo --force         # DEBUG=0（staging/UAT）显式确认
    python manage.py seed_demo --dry-run       # 只统计不写
    python manage.py seed_demo --clear         # 删除本命令写入的演示价/费率/客户
    python manage.py seed_demo --effective-from 2026-01-01

生产环境（``DEBUG=False``）默认拒绝执行；确属演示/UAT 必须显式 ``--force``。
清空只删 ``source='demo'`` 的材料价、带“演示”标记的费率与 ``demo-*`` 客户，
保留材质主数据（牌号/密度本身是标准参考数据，不是价格）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from quote import prices
from quote.models import Customer, Material, MaterialPrice, ProcessRate

DEMO_SOURCE = prices.DEMO_SOURCE
DEMO_EFFECTIVE_FROM = date(2026, 1, 1)
DEMO_NOTE = '演示价（HTML 原型示例值），禁止用于生产报价；真实价格由财务在价格库维护。'
DEMO_NOTICE = (
    '⚠️ 本命令写入的是演示数据（source=demo / 标签“（演示）”），'
    '仅用于界面联调、UAT 与测试，禁止作为正式报价依据。'
)

# 23 个材质：id/label/family/price/density/pt 与 HTML 原型 §7 一致（pt 仅记录，模型不存）。
DEMO_MATERIALS = (
    {'code': 'A105', 'label': 'ASTM A105 / A105N', 'family': '碳钢', 'price': '7.20', 'density': '7.850', 'pt': '1.1'},
    {'code': 'LF2', 'label': 'ASTM A350 LF2', 'family': '低温碳钢', 'price': '8.60', 'density': '7.850', 'pt': '1.1'},
    {'code': 'F304', 'label': 'ASTM A182 F304', 'family': '不锈钢 304', 'price': '28.00', 'density': '7.930', 'pt': '2.1'},
    {'code': 'F316L', 'label': 'ASTM A182 F316L', 'family': '不锈钢 316L', 'price': '35.00', 'density': '7.930', 'pt': '2.3'},
    {'code': 'F11', 'label': 'ASTM A182 F11', 'family': '铬钼合金钢', 'price': '10.20', 'density': '7.850', 'pt': '1.9'},
    {'code': 'F22', 'label': 'ASTM A182 F22', 'family': '铬钼合金钢', 'price': '12.00', 'density': '7.850', 'pt': '1.10'},
    {'code': 'F91', 'label': 'ASTM A182 F91', 'family': '铬钼合金钢', 'price': '16.00', 'density': '7.850', 'pt': '1.15'},
    {'code': 'F51', 'label': 'ASTM A182 F51 (双相)', 'family': '双相不锈钢', 'price': '62.00', 'density': '7.900', 'pt': None},
    {'code': 'A694F52', 'label': 'ASTM A694 F52', 'family': '碳钢', 'price': '10.50', 'density': '7.850', 'pt': None},
    {'code': 'A694F60', 'label': 'ASTM A694 F60', 'family': '碳钢', 'price': '11.50', 'density': '7.850', 'pt': None},
    {'code': 'P250GH', 'label': 'P250GH', 'family': '碳钢', 'price': '7.00', 'density': '7.850', 'pt': None},
    {'code': 'P265GH', 'label': 'P265GH', 'family': '碳钢', 'price': '7.20', 'density': '7.850', 'pt': None},
    {'code': 'S235JR', 'label': 'S235JR', 'family': '碳钢', 'price': '6.60', 'density': '7.850', 'pt': None},
    {'code': 'S355J2', 'label': 'S355J2', 'family': '低合金钢', 'price': '7.80', 'density': '7.850', 'pt': None},
    {'code': '1.4301', 'label': '1.4301 (304)', 'family': '不锈钢 304', 'price': '27.00', 'density': '7.930', 'pt': None},
    {'code': '1.4404', 'label': '1.4404 (316L)', 'family': '不锈钢 316L', 'price': '34.00', 'density': '7.930', 'pt': None},
    {'code': '1.4571', 'label': '1.4571 (316Ti)', 'family': '不锈钢 316Ti', 'price': '38.00', 'density': '7.930', 'pt': None},
    {'code': 'SS400', 'label': 'SS400', 'family': '碳钢', 'price': '6.60', 'density': '7.850', 'pt': None},
    {'code': 'S25C', 'label': 'S25C', 'family': '碳钢', 'price': '7.20', 'density': '7.850', 'pt': None},
    {'code': 'SUS304', 'label': 'SUS304', 'family': '不锈钢 304', 'price': '27.00', 'density': '7.930', 'pt': None},
    {'code': 'SUS316', 'label': 'SUS316', 'family': '不锈钢 316', 'price': '34.00', 'density': '7.930', 'pt': None},
    {'code': 'SCS13', 'label': 'SCS13 (铸件)', 'family': '不锈钢 304', 'price': '30.00', 'density': '7.930', 'pt': None},
    {'code': 'SCS14', 'label': 'SCS14 (铸件)', 'family': '不锈钢 316', 'price': '38.00', 'density': '7.930', 'pt': None},
)

# 工艺费率：code/label/unit/rate/tiers；数值与 costing.py 演示默认值一致。
DEMO_RATES = (
    {
        'code': 'machining',
        'label': '加工费（演示）',
        'unit': 'per_piece',
        'rate': '200.00',
        'tiers': {'50': 12, '100': 18, '200': 28, '350': 45, '600': 75, '1000': 120},
    },
    {
        'code': 'surface',
        'label': '表面处理费率（演示）',
        'unit': 'per_kg',
        'rate': '0.00',
        'tiers': {'none': 0, 'rust': 1.2, 'zinc': 3.0, 'paint': 3.8},
    },
    {
        'code': 'packaging',
        'label': '包装费（演示）',
        'unit': 'per_piece',
        'rate': '0.00',
        'tiers': {'none': 0, 'carton': 8, 'wood': 35, 'fumigated': 55},
    },
    {
        'code': 'inspection',
        'label': '检测费（演示）',
        'unit': 'per_piece',
        'rate': '0.00',
        'tiers': {'pmi': 25, 'ut': 80, 'hydro': 40, 'hardness': 12},
    },
    {
        'code': 'freight',
        'label': '运费（演示）',
        'unit': 'per_kg',
        'rate': '0.60',
        'tiers': {},
    },
    {
        'code': 'mgmt',
        'label': '管理费率（演示）',
        'unit': 'percent',
        'rate': '6.00',
        'tiers': {},
    },
)

# 客户 A/B/C（代码 demo-* 为演示保留命名空间）；折扣契约 0–30%。
DEMO_CUSTOMERS = (
    {
        'code': 'demo-a',
        'name': '示例客户 A（演示）',
        'level': 'A',
        'discount_pct': '8.00',
        'currency': 'CNY',
        'payment_terms': '30% 预付，余款发货前',
        'active': True,
    },
    {
        'code': 'demo-b',
        'name': '示例客户 B（演示）',
        'level': 'B',
        'discount_pct': '4.00',
        'currency': 'CNY',
        'payment_terms': '30% 预付，余款见提单副本',
        'active': True,
    },
    {
        'code': 'demo-c',
        'name': '示例客户 C（演示）',
        'level': 'C',
        'discount_pct': '0.00',
        'currency': 'CNY',
        'payment_terms': '款到发货',
        'active': True,
    },
)

DEMO_RATE_CODES = tuple(rate['code'] for rate in DEMO_RATES)
DEMO_CUSTOMER_CODES = tuple(customer['code'] for customer in DEMO_CUSTOMERS)


def _is_demo_rate(rate: ProcessRate) -> bool:
    """判定一条费率是否为本命令写入的演示费率（兼容 source 字段上线前后）。"""
    if getattr(rate, 'source', '') == DEMO_SOURCE:
        return True
    return '演示' in (rate.label or '')


class Command(BaseCommand):
    help = (
        '写入演示种子数据：23 个材质 + 材料价（source=demo）、'
        '工艺费率、包装/检测/运费、客户 A/B/C；幂等，禁止当真实价上线。'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='DEBUG=False（staging/UAT/生产）时显式确认写入演示数据',
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='删除本命令写入的演示材料价/费率/客户（保留材质主数据）',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='只统计将写入的数据，不写数据库',
        )
        parser.add_argument(
            '--effective-from',
            default=DEMO_EFFECTIVE_FROM.isoformat(),
            help=f'演示材料价生效日期（默认 {DEMO_EFFECTIVE_FROM.isoformat()}）',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options['force']:
            raise CommandError(
                '当前 DEBUG=False（疑似生产环境）。seed_demo 写入的是演示价格，'
                '禁止静默进入生产；确属 UAT/演示请加 --force。'
            )

        effective_from = self._parse_effective_date(options['effective_from'])
        self.stdout.write(self.style.WARNING(DEMO_NOTICE))

        if options['clear']:
            stats = self._clear()
            self._report_clear(stats)
            return

        if options['dry_run']:
            self.stdout.write(
                f'[dry-run] 将写入材质 {len(DEMO_MATERIALS)} 个、材料价格 '
                f'{len(DEMO_MATERIALS)} 条（生效 {effective_from}，source=demo）、'
                f'工艺费率 {len(DEMO_RATES)} 条、客户 {len(DEMO_CUSTOMERS)} 个；未写数据库。'
            )
            return

        with transaction.atomic():
            stats = self._seed(effective_from)
        self._report(stats, effective_from)

    # ------------------------------------------------------------------ 写入
    def _seed(self, effective_from: date) -> dict:
        stats = {
            'material_created': 0,
            'material_skipped': 0,
            'price_created': 0,
            'price_updated': 0,
            'price_skipped': 0,
            'rate_created': 0,
            'rate_updated': 0,
            'rate_skipped': 0,
            'customer_created': 0,
            'customer_updated': 0,
        }

        materials = {}
        for sort_order, spec in enumerate(DEMO_MATERIALS):
            material, created = Material.objects.get_or_create(
                code=spec['code'],
                defaults={
                    'label': spec['label'],
                    'family': spec['family'],
                    'density': Decimal(spec['density']),
                    'active': True,
                    'sort_order': sort_order,
                },
            )
            # 已存在的材质不覆盖（可能是人工维护过的真实主数据）
            if created:
                stats['material_created'] += 1
            else:
                stats['material_skipped'] += 1
            materials[spec['code']] = material

        for spec in DEMO_MATERIALS:
            material = materials[spec['code']]
            existing = MaterialPrice.objects.filter(
                material=material, effective_from=effective_from
            ).first()
            if existing is not None and existing.source != DEMO_SOURCE:
                # 已有真实采购/市场价：不用演示值覆盖
                stats['price_skipped'] += 1
                continue
            _, created = MaterialPrice.objects.update_or_create(
                material=material,
                effective_from=effective_from,
                defaults={
                    'price_per_kg': Decimal(spec['price']),
                    'currency': 'CNY',
                    'source': DEMO_SOURCE,
                    'note': DEMO_NOTE,
                },
            )
            if created:
                stats['price_created'] += 1
            else:
                stats['price_updated'] += 1

        for spec in DEMO_RATES:
            existing = ProcessRate.objects.filter(code=spec['code']).first()
            if existing is not None and not _is_demo_rate(existing):
                stats['rate_skipped'] += 1
                continue
            defaults = {
                'label': spec['label'],
                'unit': spec['unit'],
                'rate': Decimal(spec['rate']),
                'tiers': spec['tiers'],
                'effective_from': effective_from,
                'source': DEMO_SOURCE,
            }
            _, created = ProcessRate.objects.update_or_create(
                code=spec['code'], defaults=defaults
            )
            if created:
                stats['rate_created'] += 1
            else:
                stats['rate_updated'] += 1

        for spec in DEMO_CUSTOMERS:
            _, created = Customer.objects.update_or_create(
                code=spec['code'],
                defaults={
                    'name': spec['name'],
                    'level': spec['level'],
                    'discount_pct': Decimal(spec['discount_pct']),
                    'currency': spec['currency'],
                    'payment_terms': spec['payment_terms'],
                    'active': spec['active'],
                },
            )
            if created:
                stats['customer_created'] += 1
            else:
                stats['customer_updated'] += 1

        return stats

    # ------------------------------------------------------------------ 清理
    def _clear(self) -> dict:
        price_deleted = MaterialPrice.objects.filter(source=DEMO_SOURCE).delete()[0]
        rate_ids = [
            rate.pk
            for rate in ProcessRate.objects.filter(code__in=DEMO_RATE_CODES)
            if _is_demo_rate(rate)
        ]
        rate_deleted = (
            ProcessRate.objects.filter(pk__in=rate_ids).delete()[0] if rate_ids else 0
        )
        customer_deleted = Customer.objects.filter(code__in=DEMO_CUSTOMER_CODES).delete()[0]
        return {
            'price': price_deleted,
            'rate': rate_deleted,
            'customer': customer_deleted,
        }

    # ------------------------------------------------------------------ 输出
    def _report(self, stats: dict, effective_from: date):
        self.stdout.write(
            f'材质：新建 {stats["material_created"]}，已存在跳过 {stats["material_skipped"]}'
            f'（共 {len(DEMO_MATERIALS)} 个演示牌号）'
        )
        self.stdout.write(
            f'材料价格：新建 {stats["price_created"]}，更新 {stats["price_updated"]}，'
            f'跳过（已有非演示价）{stats["price_skipped"]}；'
            f'生效日期 {effective_from}，source={DEMO_SOURCE}'
        )
        self.stdout.write(
            f'工艺费率：新建 {stats["rate_created"]}，更新 {stats["rate_updated"]}，'
            f'跳过（已有非演示费率）{stats["rate_skipped"]}；'
            f'共 {len(DEMO_RATES)} 条'
        )
        self.stdout.write(
            f'客户：新建 {stats["customer_created"]}，更新 {stats["customer_updated"]}；'
            f'共 {len(DEMO_CUSTOMERS)} 个（A/B/C）'
        )
        self.stdout.write(self.style.SUCCESS('演示种子数据写入完成。'))
        self.stdout.write(
            '演示价 / 演示费率已显式标注 source=demo；报价快照会标 '
            'demo_rates=True、rate_source=demo_defaults（或 demo_mixed），'
            '不会被误认为真实价格。'
        )

    def _report_clear(self, stats: dict):
        self.stdout.write(
            f'已删除演示数据：材料价 {stats["price"]} 条、'
            f'工艺费率 {stats["rate"]} 条、客户 {stats["customer"]} 个；'
            '材质主数据保留（如需删除请人工确认后操作）。'
        )
        self.stdout.write(self.style.SUCCESS('演示数据清理完成。'))

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _parse_effective_date(value) -> date:
        text = str(value or '').strip()
        if not text:
            return DEMO_EFFECTIVE_FROM
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y%m%d'):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        raise CommandError(f'--effective-from 日期格式非法：{text!r}（用 YYYY-MM-DD）')
