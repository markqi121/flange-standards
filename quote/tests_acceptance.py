"""独立验收测试（T4 / qa-release，2026-11-06）。

目标：回答"是否达到可上线标准"，而不是重复开发者单测。覆盖任务书 A–J：

- A 1,348 条 SKU 全量选型：options 级联覆盖、逐行重量/校验/计算冒烟；
- B 20 张历史报价回放：成本/单价/税额/总额/快照可回放、金额一致；
- C 成本/定价/重量/校验：边界值、p/T、无参考重量 C 级、组合缺失、Decimal 舍入；
- D 权限矩阵：匿名 / 销售 / 技术 / 财务 / 经理 / 管理员；
- E 状态机：draft→pending/approved→sent→accepted→ordered、reject/expire/cancel、版本/快照；
- F 页面：工作台/列表/详情/打印/PDF（Playwright 缺失时 503 降级）、A4、CSRF；
- G 安全：DEBUG/SECRET_KEY/ALLOWED_HOSTS、admin 保护、成本不泄露；
- H 性能：全量选项/计算、工作台响应（SQLite 并发见 scripts/acceptance/perf_smoke.py）；
- I 备份/恢复：script 实测 + 文档清单；
- J 部署门：见 docs/上线验收报告.md。

验收数据直接使用仓库数据包：``import_standards`` 导入 1,348 条规格，
``seed_demo`` 写入演示价格/费率/客户（均标记 source=demo）。

运行：
    .venv/bin/python manage.py test quote.tests_acceptance -v 2

注意：本文件只做验收，不修改业务代码；失败项是上线阻塞证据，不做"跳过/伪造通过"。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command, get_commands
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import load_workbook

from catalog.models import DimensionRow, FlangeType, Rating, Standard

from quote import costing, permissions, pricing, prices, services, validation, weight
from quote.models import (
    ApprovalLog,
    CostSnapshot,
    Customer,
    Material,
    MaterialPrice,
    ProcessRate,
    Quotation,
    QuotationItem,
)

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
QUOTE_SCRIPT_DIR = BASE_DIR / 'scripts' / 'acceptance'
FULL_CATALOG_COUNT = 1348
ON_DATE = '2026-01-15'
VALID_UNTIL = '2026-01-30'
MONEY_Q = Decimal('0.01')

# 契约 §5 角色（Django Group 名）
ROLE_GROUPS = ('sales', 'engineering', 'finance', 'sales_manager', 'quote_admin')

# 各标准默认选用一个"在演示价格库中有价格"的材质（材质本身不限制标准）
STANDARD_MATERIAL = {
    'asme-b16-5': 'A105',
    'asme-b16-47-a': 'A105',
    'asme-b16-47-b': 'A105',
    'en-1092-1': 'P250GH',
    'jis-b2220': 'SS400',
}
ALT_MATERIAL = {
    'asme-b16-5': 'F316L',
    'asme-b16-47-a': 'F304',
    'asme-b16-47-b': 'F51',
    'en-1092-1': '1.4301',
    'jis-b2220': 'SUS304',
}


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def payload_for(row: DimensionRow, material=None, **overrides) -> dict:
    """按一行 catalog.DimensionRow 生成契约 §3.2 输入。"""
    material_code = material or STANDARD_MATERIAL.get(row.standard_id, 'A105')
    payload = {
        'std': row.standard_id,
        'type': row.flange_type_id,
        'size': row.size,
        'rating': row.rating.code,
        'material': material_code,
        'qty': 10,
        'design_temp_c': 38,
        'design_pressure_bar': 16,
        'surface': 'none',
        'packaging': 'wood',
        'inspections': [],
        'freight_per_kg': 0.6,
        'discount_pct': 0,
        'currency': 'CNY',
        'fx_rate': 7.10,
        'on_date': ON_DATE,
        'valid_until': VALID_UNTIL,
        'params': {
            'yield': 0.70,
            'machining_factor': 1.0,
            'management_pct': 6,
            'profit_pct': 12,
            'tax_pct': 13,
        },
    }
    payload.update(overrides)
    return payload


class AcceptanceDataMixin:
    """导入真实标准库数据 + 演示价格种子；所有验收类共用。"""

    @classmethod
    def setUpTestData(cls):
        call_command('import_standards', verbosity=0)
        call_command('seed_demo', force=True, verbosity=0)
        cls.rows = list(
            DimensionRow.objects.select_related('standard', 'flange_type', 'rating').order_by('pk')
        )
        cls.materials = {material.code: material for material in Material.objects.all()}
        cls.user = User.objects.create_user('qa-user', password='qa-pass-12345')
        cls.admin = User.objects.create_superuser('qa-admin', 'qa-admin@example.com', 'qa-pass-12345')

    def login(self, user=None):
        self.client.force_login(user or self.user)

    def assert_quote_amount_consistency(self, quote: Quotation):
        """契约 §3.2：金额 = 明细 × 数量 + 税，四舍五入一致。"""
        subtotal = Decimal('0')
        tax_total = Decimal('0')
        for item in quote.items.all():
            breakdown = item.cost_breakdown or {}
            price = breakdown.get('price') or {}
            self.assertTrue(price, f'{quote.number} 明细缺少价格快照')
            unit_net = money(price['unit_net'])
            unit_tax = money(price['unit_tax'])
            unit_gross = money(price['unit_gross'])
            amount_net = money(price['amount_net'])
            tax_amount = money(price['tax_amount'])
            total_gross = money(price['total_gross'])
            qty = item.qty

            # 单件：含税 = 不含税 + 税（先算单件，再乘数量）
            self.assertEqual(unit_gross, unit_net + unit_tax,
                             f'{quote.number} 单价含税 != 不含税 + 税')
            self.assertEqual(amount_net, money(unit_net * qty),
                             f'{quote.number} 金额 != 单价×数量（四舍五入 2 位）')
            self.assertEqual(tax_amount, money(unit_tax * qty),
                             f'{quote.number} 税额 != 单件税×数量')
            self.assertEqual(total_gross, amount_net + tax_amount,
                             f'{quote.number} 含税总额 != 金额 + 税额')

            # 持久化字段与价格快照一致
            self.assertEqual(item.amount, amount_net, f'{quote.number} 明细金额与快照不一致')
            self.assertEqual(item.unit_price, unit_gross, f'{quote.number} 含税单价与快照不一致')
            subtotal += item.amount
            tax_total += tax_amount

        self.assertEqual(quote.subtotal, subtotal, f'{quote.number} 整单不含税 != 明细金额和')
        self.assertEqual(quote.tax_amount, tax_total, f'{quote.number} 整单税额 != 明细税额和')
        self.assertEqual(quote.total, quote.subtotal + quote.tax_amount,
                         f'{quote.number} 整单含税 != 不含税 + 税额')


# ============================================================================
# A. 1,348 条 SKU 全量选型
# ============================================================================
class FullCatalogSweepTests(AcceptanceDataMixin, TestCase):
    """A：对每个 catalog.DimensionRow 至少 resolve / 校验一次。"""

    def test_a1_catalog_is_1348_unique_combinations(self):
        self.assertEqual(DimensionRow.objects.count(), FULL_CATALOG_COUNT)
        self.assertEqual(Standard.objects.count(), 5)
        self.assertEqual(FlangeType.objects.count(), 3)
        from django.db.models import Count

        combos = (
            DimensionRow.objects.values('standard_id', 'flange_type_id', 'rating_id', 'size')
            .annotate(n=Count('id'))
        )
        dup = [c for c in combos if c['n'] > 1]
        self.assertFalse(dup, f'存在重复规格组合：{dup[:5]}')

    def test_a2_options_cascade_covers_every_row(self):
        options = services.catalog_options()
        seen = []
        for std_id, types in options['catalog'].items():
            for type_id, ratings in types.items():
                for rating_code, sizes in ratings.items():
                    for entry in sizes:
                        seen.append((std_id, type_id, rating_code, entry['size']))
        self.assertEqual(len(seen), FULL_CATALOG_COUNT)
        self.assertEqual(len(set(seen)), FULL_CATALOG_COUNT, '级联选项存在重复项')
        db_combos = set(
            DimensionRow.objects.values_list('standard_id', 'flange_type_id', 'rating__code', 'size')
        )
        self.assertEqual(db_combos, set(seen), '级联选项与数据库规格不一致（有缺失或多余）')
        # 材质/工艺/默认参数也必须随 options 返回
        self.assertEqual(len(options['materials']), 23)
        self.assertTrue(options['surfaces'] and options['packaging'] and options['inspections'])
        self.assertEqual(float(options['defaults']['tax_pct']), 13.0)

    def test_a3_every_row_resolves_weight(self):
        errors = []
        sources = Counter()
        confidences = Counter()
        for row in self.rows:
            material = self.materials[STANDARD_MATERIAL[row.standard_id]]
            try:
                result = weight.resolve_weight(row, material)
            except Exception as exc:  # noqa: BLE001 - 汇总后统一断言
                errors.append(f'{row.pk} {row.standard_id}/{row.flange_type_id}/'
                              f'{row.rating.code}/{row.size}: {exc!r}')
                continue
            sources[result.source] += 1
            confidences[result.confidence] += 1
            if not (result.net_weight > 0):
                errors.append(f'{row.pk} 净重非正：{result.net_weight}')
            if result.rule_version != 'w0.3':
                errors.append(f'{row.pk} 重量规则版本错误：{result.rule_version}')
            if result.source == 'standard' and result.confidence != 'B':
                errors.append(f'{row.pk} 参考重量置信度应为 B，实际 {result.confidence}')
            if result.source == 'geometry' and result.confidence != 'C':
                errors.append(f'{row.pk} 几何重量置信度应为 C，实际 {result.confidence}')
        self.assertEqual(sum(sources.values()), FULL_CATALOG_COUNT, f'重量未覆盖全部行：{errors[:10]}')
        self.assertFalse(errors, '重量计算失败：\n' + '\n'.join(errors[:20]))
        expected_standard = DimensionRow.objects.filter(weight__isnull=False).count()
        expected_geometry = DimensionRow.objects.filter(weight__isnull=True).count()
        self.assertEqual(sources['standard'], expected_standard)
        self.assertEqual(sources['geometry'], expected_geometry)
        self.assertEqual(sources['standard'] + sources['geometry'], FULL_CATALOG_COUNT)

    def test_a4_every_row_runs_full_calculation_pipeline(self):
        """全量跑 重量→成本→定价→p/T→校验（不落报价单）。"""
        errors = []
        statuses = Counter()
        started = time.perf_counter()
        for row in self.rows:
            payload = payload_for(row, on_date=ON_DATE)
            try:
                result = services.calculate(payload)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{row.pk} {row.standard_id}/{row.flange_type_id}/'
                              f'{row.rating.code}/{row.size}: {exc!r}')
                continue
            statuses[result['validation']['status']] += 1
            if result['validation']['status'] == validation.STATUS_FAIL:
                errors.append(f'{row.pk} 默认参数下校验失败：{result["validation"]["messages"]}')
            if not (result['price']['unit_gross'] > 0):
                errors.append(f'{row.pk} 含税单价非正')
            if result['item']['weight_source'] not in ('standard', 'geometry'):
                errors.append(f'{row.pk} 重量来源异常：{result["item"]["weight_source"]}')
            if result['snapshot']['weight_rule'] != 'w0.3':
                errors.append(f'{row.pk} 快照重量规则异常')
        elapsed = time.perf_counter() - started
        self.assertFalse(errors, '全量计算失败：\n' + '\n'.join(errors[:20]))
        self.assertEqual(sum(statuses.values()), FULL_CATALOG_COUNT)
        self.assertNotIn(validation.STATUS_FAIL, statuses, f'存在校验 fail 的 SKU：{statuses}')
        self.assertLess(elapsed, 60.0, f'全量计算耗时 {elapsed:.1f}s 超过 60s')

    def test_a5_missing_combination_returns_400_contract(self):
        self.login()
        payload = payload_for(self.rows[0], type='slip-on', rating='2500')
        response = self.client.post(
            reverse('quote:api_calc'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['error'], 'invalid_combination')
        self.assertEqual(body['field'], 'size')


# ============================================================================
# B. 20 张历史报价回放
# ============================================================================
def _pick_quote_rows(rows, count=20):
    """按 (标准/类型/等级) 分组，跨组均匀取 count 行，保证规格覆盖。"""
    groups = {}
    for row in rows:
        groups.setdefault((row.standard_id, row.flange_type_id, row.rating.code), []).append(row)
    keys = sorted(groups)
    step = max(1, len(keys) // count)
    picks = []
    index = 0
    while index < len(keys) and len(picks) < count:
        bucket = groups[keys[index]]
        picks.append(bucket[len(bucket) // 2])
        index += step
    index = 0
    while len(picks) < count and index < len(keys):
        row = groups[keys[index]][0]
        if row not in picks:
            picks.append(row)
        index += 1
    return picks[:count]


# 20 组不同规格/材质/数量/工艺/折扣组合
QUOTE_VARIANTS = [
    dict(qty=1, surface='none', packaging='none', inspections=[], discount_pct=0),
    dict(qty=2, surface='rust', packaging='carton', inspections=['pmi'], discount_pct=0),
    dict(qty=5, surface='zinc', packaging='wood', inspections=['ut'], discount_pct=0),
    dict(qty=10, surface='paint', packaging='fumigated', inspections=['hydro'], discount_pct=2),
    dict(qty=20, surface='none', packaging='wood', inspections=['hardness'], discount_pct=0),
    dict(qty=50, surface='rust', packaging='carton', inspections=['third'], discount_pct=3),
    dict(qty=100, surface='zinc', packaging='wood', inspections=['pmi', 'third'], discount_pct=4),
    dict(qty=3, surface='paint', packaging='none', inspections=['ut', 'hydro'], discount_pct=0),
    dict(qty=7, surface='none', packaging='fumigated', inspections=[], discount_pct=5),
    dict(qty=12, surface='rust', packaging='wood', inspections=['pmi'], discount_pct=6),
    dict(qty=25, surface='zinc', packaging='carton', inspections=['third'], discount_pct=0),
    dict(qty=40, surface='paint', packaging='wood', inspections=['hydro'], discount_pct=1),
    dict(qty=8, surface='none', packaging='none', inspections=['hardness'], discount_pct=0),
    dict(qty=15, surface='rust', packaging='fumigated', inspections=['ut'], discount_pct=2),
    dict(qty=60, surface='zinc', packaging='wood', inspections=['pmi', 'hydro'], discount_pct=3),
    dict(qty=9, surface='paint', packaging='carton', inspections=['third'], discount_pct=4),
    dict(qty=30, surface='none', packaging='wood', inspections=[], discount_pct=5),
    dict(qty=4, surface='rust', packaging='carton', inspections=['pmi'], discount_pct=6),
    dict(qty=18, surface='zinc', packaging='fumigated', inspections=['ut', 'third'], discount_pct=0),
    dict(qty=75, surface='paint', packaging='wood', inspections=['hydro'], discount_pct=10,
         currency='USD', fx_rate=7.0),
]


class HistoricalQuoteReplayTests(AcceptanceDataMixin, TestCase):
    """B：20 张不同规格/材质/数量/工艺的历史报价，金额一致且快照可回放。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cases = []
        for index, row in enumerate(_pick_quote_rows(cls.rows, 20)):
            variant = dict(QUOTE_VARIANTS[index])
            material = STANDARD_MATERIAL[row.standard_id]
            if index % 5 == 0:
                material = ALT_MATERIAL[row.standard_id]
            cls.cases.append((row, material, variant))

    def test_b1_twenty_quotes_are_created_with_consistent_amounts(self):
        self.assertEqual(len(self.cases), 20, '历史报价回放样本不足 20 组')
        numbers = set()
        for row, material, variant in self.cases:
            payload = payload_for(row, material=material, **variant)
            quote = services.create_quote(payload, self.user)
            numbers.add(quote.number)
            self.assertEqual(quote.items.count(), 1)
            item = quote.items.select_related('dimension_row', 'material').first()
            self.assertEqual(item.dimension_row_id, row.pk)
            self.assertEqual(item.material.code, material)
            self.assertEqual(item.qty, variant['qty'])
            self.assertEqual(quote.status, Quotation.Status.DRAFT)
            self.assertEqual(quote.version, 1)
            self.assertRegex(quote.number, r'^FQ-\d{4}-\d{4}$')
            self.assertIsNotNone(quote.snapshot)
            self.assert_quote_amount_consistency(quote)
            # 快照基本口径
            snapshot = quote.snapshot
            self.assertEqual(snapshot.weight_rule, 'w0.3')
            self.assertEqual(snapshot.fx_rate, Decimal(str(variant.get('fx_rate', 7.10))))
            self.assertEqual(quote.currency, variant.get('currency', 'CNY'))
            self.assertEqual(snapshot.params['material_price_effective_from'], '2026-01-01')
            self.assertIn(row.standard_id, snapshot.standard_editions)
            self.assertIn(material, snapshot.price_versions['material_prices'])
        self.assertEqual(len(numbers), 20, '报价编号重复')

    def test_b2_replay_from_input_matches_stored_snapshot(self):
        for row, material, variant in self.cases:
            payload = payload_for(row, material=material, **variant)
            quote = services.create_quote(payload, self.user)
            item = quote.items.first()
            replay = services.calculate(payload)
            self.assertEqual(replay['costs'], item.cost_breakdown['costs'],
                             f'{quote.number} 成本回放不一致')
            self.assertEqual(replay['price'], item.cost_breakdown['price'],
                             f'{quote.number} 价格回放不一致')
            self.assertEqual(replay['item']['net_weight'], float(item.net_weight))
            self.assertEqual(replay['item']['weight_source'], item.weight_source)
            self.assertEqual(replay['item']['weight_confidence'], item.weight_confidence)
            self.assertEqual(replay['item']['standard_edition'], item.standard_edition)
            self.assertNotEqual(replay['validation']['status'], validation.STATUS_FAIL)

    def test_b3_replay_from_frozen_snapshot_recomputes_price(self):
        """不依赖数据库价格：直接用明细里的冻结成本 + 冻结定价参数复算。"""
        for row, material, variant in self.cases:
            payload = payload_for(row, material=material, **variant)
            quote = services.create_quote(payload, self.user)
            item = quote.items.first()
            snapshot_params = quote.snapshot.params
            frozen = item.cost_breakdown['price']
            replay = pricing.compute_price(
                unit_cost=item.cost_breakdown['unit_cost_raw'],
                qty=item.qty,
                profit_pct=snapshot_params['profit_pct'],
                discount_pct=snapshot_params['discount_pct'],
                tax_pct=snapshot_params['tax_pct'],
                net_weight=item.net_weight,
                currency=quote.currency,
                fx_rate=quote.fx_rate,
            ).as_dict()
            for key in ('unit_net', 'unit_tax', 'unit_gross', 'amount_net',
                        'tax_amount', 'total_gross', 'margin_pct'):
                self.assertEqual(replay[key], frozen[key],
                                 f'{quote.number} 冻结快照复算 {key} 不一致')

    def test_b4_price_change_does_not_rewrite_history(self):
        row, material, variant = self.cases[0]
        payload = payload_for(row, material=material, **variant)
        quote = services.create_quote(payload, self.user)
        total_before = quote.total
        item = quote.items.first()
        cost_before = dict(item.cost_breakdown['costs'])

        MaterialPrice.objects.create(
            material=item.material,
            price_per_kg=Decimal('99.99'),
            effective_from=date(2026, 6, 1),
            source='purchase',
        )
        quote.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(quote.total, total_before, '新增材料价后历史报价总额被改写')
        self.assertEqual(item.cost_breakdown['costs'], cost_before, '历史报价成本快照被改写')

        replay_old = services.calculate(payload)
        self.assertEqual(replay_old['costs'], cost_before,
                         '按原报价日回放取到了新价格（生效日期失效）')
        future_payload = dict(payload, on_date='2026-07-01', valid_until='2026-07-16')
        replay_new = services.calculate(future_payload)
        self.assertGreater(replay_new['costs']['material'], replay_old['costs']['material'],
                           '新价格生效后按新日期回放应取到新价格')
        self.assertEqual(pricing.price_for(item.material, date(2026, 1, 15)).price_per_kg,
                         item.material_price)
        self.assertEqual(pricing.price_for(item.material, date(2026, 7, 1)).price_per_kg,
                         Decimal('99.99'))

    def test_b5_multi_item_quote_totals(self):
        selected = self.cases[:3]
        items = [
            payload_for(row, material=material, **{
                key: value for key, value in variant.items()
                if key in ('qty', 'surface', 'packaging', 'inspections', 'discount_pct')
            })
            for row, material, variant in selected
        ]
        payload = {
            'customer': 'demo-a',
            'incoterm': 'FOB',
            'items': items,
            'on_date': ON_DATE,
            'valid_until': VALID_UNTIL,
            'params': {'yield': 0.70, 'machining_factor': 1.0,
                       'management_pct': 6, 'profit_pct': 12, 'tax_pct': 13},
        }
        quote = services.create_quote(payload, self.user)
        self.assertEqual(quote.items.count(), 3)
        self.assertEqual(quote.customer.code, 'demo-a')
        self.assert_quote_amount_consistency(quote)
        snapshot = quote.snapshot.params
        self.assertEqual(len(snapshot['items']), 3)
        self.assertAlmostEqual(snapshot['fx_rate'], 7.10, places=4)
        data = services.quote_to_dict(quote)
        self.assertEqual(len(data['items']), 3)
        self.assertEqual(money(data['total']), quote.total)

    def test_b6_quote_number_sequence(self):
        first = services.create_quote(payload_for(self.rows[0]), self.user)
        second = services.create_quote(payload_for(self.rows[1]), self.user)
        self.assertRegex(first.number, r'^FQ-\d{4}-\d{4}$')
        self.assertEqual(int(second.number[-4:]), int(first.number[-4:]) + 1)


# ============================================================================
# C. 成本 / 定价 / 重量 / 校验边界
# ============================================================================
class CostPricingEdgeCaseTests(AcceptanceDataMixin, TestCase):
    def setUp(self):
        # 用契约 §9 的 B16.5 4" Class 150 WN 行：p/T 已接入，便于验证 pass/warn/fail 边界
        self.row = DimensionRow.objects.get(
            standard_id='asme-b16-5', flange_type_id='weld-neck',
            size='4', rating__code='150')

    def test_c1_qty_boundaries(self):
        with self.assertRaises(services.InvalidPayload):
            services.calculate(payload_for(self.row, qty=0))
        with self.assertRaises(services.InvalidPayload):
            services.calculate(payload_for(self.row, qty=-3))
        result = services.calculate(payload_for(self.row, qty=1))
        self.assertEqual(result['item']['qty'], 1)
        # 超大批量：不阻断，但校验必须警告（人工复核）
        result = services.calculate(payload_for(self.row, qty=10001))
        self.assertEqual(result['validation']['status'], validation.STATUS_WARN)
        self.assertTrue(any('人工复核' in msg for msg in result['validation']['messages']))

    def test_c2_discount_boundaries(self):
        result = services.calculate(payload_for(self.row, discount_pct=5))
        self.assertEqual(result['validation']['status'], validation.STATUS_PASS)
        result = services.calculate(payload_for(self.row, discount_pct=6))
        self.assertEqual(result['validation']['status'], validation.STATUS_WARN)
        result = services.calculate(payload_for(self.row, discount_pct=50))
        self.assertEqual(result['validation']['status'], validation.STATUS_WARN)
        with self.assertRaises(services.InvalidPayload):
            services.calculate(payload_for(self.row, discount_pct=50.01))
        with self.assertRaises(services.InvalidPayload):
            services.calculate(payload_for(self.row, discount_pct=-1))

    def test_c3_pt_over_temperature_and_pressure(self):
        self.assertEqual(
            validation.validate_pt('A105', '150', 38, 16)['status'], validation.STATUS_PASS)
        # 接近上限（>90%）→ warn；超压 → fail
        self.assertEqual(
            validation.validate_pt('A105', '150', 38, 19.6)['status'], validation.STATUS_WARN)
        over_pressure = validation.validate_pt('A105', '150', 38, 20)
        self.assertEqual(over_pressure['status'], validation.STATUS_FAIL)
        self.assertLess(over_pressure['allowed_bar'], 20)
        self.assertEqual(over_pressure['material_group'], '1.1')
        over_temp = validation.validate_pt('A105', '150', 500, 16)
        self.assertEqual(over_temp['status'], validation.STATUS_FAIL)
        low_temp = validation.validate_pt('A105', '150', -40, 16)
        self.assertTrue(any('低温冲击' in message for message in low_temp['messages']),
                        f'低于 -29°C 未提示低温冲击：{low_temp}')
        # 计算入口：超压时校验 fail，create_quote 必须拒绝落库
        fail_calc = services.calculate(payload_for(self.row, design_pressure_bar=20))
        self.assertEqual(fail_calc['validation']['status'], validation.STATUS_FAIL)
        with self.assertRaises(services.ValidationFailed):
            services.create_quote(payload_for(self.row, design_pressure_bar=20), self.user)

    def test_c4_weight_b_and_c_sources(self):
        standard_row = DimensionRow.objects.filter(weight__isnull=False).first()
        geometry_row = DimensionRow.objects.filter(weight__isnull=True).first()
        self.assertIsNotNone(standard_row)
        self.assertIsNotNone(geometry_row)
        material = self.materials['A105']
        b_result = weight.resolve_weight(standard_row, material)
        self.assertEqual((b_result.source, b_result.confidence), ('standard', 'B'))
        c_result = weight.resolve_weight(geometry_row, material)
        self.assertEqual((c_result.source, c_result.confidence), ('geometry', 'C'))
        self.assertGreater(c_result.net_weight, 0)
        # 密度修正：同一行用不锈钢密度，参考重量按 7.93/7.85 放大
        stainless = self.materials['F316L']
        b_stainless = weight.resolve_weight(standard_row, stainless)
        expected = (Decimal(str(standard_row.weight)) * stainless.density
                    / Decimal('7.85')).quantize(Decimal('0.001'))
        self.assertEqual(b_stainless.net_weight, expected)

    def test_c5_geometry_insufficient_data_raises_weight_error(self):
        broken = DimensionRow(
            standard=self.row.standard,
            flange_type=self.row.flange_type,
            rating=self.row.rating,
            size='broken',
            size_label='BROKEN',
            dn=0,
            od=None,
            th=None,
        )
        with self.assertRaises(weight.WeightError):
            weight.resolve_weight(broken, self.materials['A105'])

    def test_c6_missing_combination_service_error(self):
        with self.assertRaises(services.InvalidCombination) as ctx:
            services.calculate(payload_for(self.row, type='slip-on', rating='2500'))
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertEqual(ctx.exception.code, 'invalid_combination')

    def test_c7_decimal_rounding_consistency(self):
        # 金额工具统一 ROUND_HALF_UP 保留 2 位
        self.assertEqual(costing.money(Decimal('1.005')), Decimal('1.01'))
        self.assertEqual(costing.money(Decimal('2.674')), Decimal('2.67'))
        self.assertEqual(pricing.money(Decimal('1.005')), Decimal('1.01'))
        for qty in (1, 2, 3, 7, 10, 33):
            result = services.calculate(payload_for(self.row, qty=qty))
            price = result['price']
            unit_net = money(price['unit_net'])
            unit_tax = money(price['unit_tax'])
            self.assertEqual(money(price['unit_gross']), unit_net + unit_tax)
            self.assertEqual(money(price['amount_net']), money(unit_net * qty))
            self.assertEqual(money(price['tax_amount']), money(unit_tax * qty))
            self.assertEqual(money(price['total_gross']),
                             money(price['amount_net']) + money(price['tax_amount']))

    def test_c8_params_validation(self):
        invalid_cases = [
            {'yield': 0}, {'yield': 1.01}, {'machining_factor': 0},
            {'management_pct': -1}, {'tax_pct': -1}, {'tax_pct': 101},
            {'fx_rate': 0}, {'freight_per_kg': -0.1}, {'currency': 'EUR'},
        ]
        for params in invalid_cases:
            with self.subTest(params=params):
                with self.assertRaises(services.InvalidPayload):
                    services.calculate(payload_for(self.row, params=params))

    def test_c9_demo_prices_must_be_marked(self):
        """演示参数必须显式标注，不能静默当真实价格。"""
        result = services.calculate(payload_for(self.row))
        meta = result['snapshot']['params']
        self.assertTrue(meta['demo_rates'], '演示材料价/费率未标记 demo_rates')
        self.assertEqual(meta['rate_source'], 'demo_defaults')
        self.assertEqual(meta['material_price_source'], 'demo')
        demo_codes = set(meta.get('demo_rate_codes') or [])
        self.assertIn('machining', demo_codes)
        self.assertIn('surface', demo_codes)
        self.assertIn('packaging', demo_codes)
        self.assertIn('freight', demo_codes)
        self.assertGreaterEqual(len(demo_codes), 5)
        quote = services.create_quote(payload_for(self.row), self.user)
        self.assertTrue(quote.snapshot.params['demo_rates'])
        self.assertTrue(quote.snapshot.params['demo_material_price'])

    def test_c10_customer_discount_data(self):
        customer = Customer.objects.get(code='demo-a')
        self.assertEqual(prices.customer_discount_pct(customer), Decimal('8.00'))
        self.assertEqual(prices.resolve_discount(customer, Decimal('2')), Decimal('10.00'))
        source = prices.discount_source(customer, Decimal('2'))
        self.assertEqual(source['customer_discount_pct'], Decimal('8.00'))
        self.assertEqual(source['extra_discount_pct'], Decimal('2.00'))
        self.assertEqual(source['total_discount_pct'], Decimal('10.00'))


# ============================================================================
# D. 权限矩阵
# ============================================================================
class PermissionMatrixTests(AcceptanceDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command('seed_roles', verbosity=0)
        cls.role_users = {}
        for role in ROLE_GROUPS:
            user = User.objects.create_user(f'qa-{role}', password='qa-pass-12345')
            user.groups.add(Group.objects.get(name=role))
            cls.role_users[role] = user
        # admin 后台可操作性需要 is_staff
        cls.staff_users = {}
        for role in ('sales', 'finance', 'quote_admin'):
            user = User.objects.create_user(
                f'qa-{role}-staff', password='qa-pass-12345', is_staff=True
            )
            user.groups.add(Group.objects.get(name=role))
            cls.staff_users[role] = user

    def _calc(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse('quote:api_calc'),
            data=json.dumps(payload_for(self.rows[0])),
            content_type='application/json',
        )

    def test_d1_anonymous_cannot_access_quote(self):
        self.client.logout()
        page_names = [
            ('quote:index', None),
            ('quote:list', None),
            ('quote:prices', None),
            ('quote:customers', None),
            ('quote:detail', ['FQ-0000-0000']),
            ('quote:print', ['FQ-0000-0000']),
            ('quote:pdf', ['FQ-0000-0000']),
        ]
        for name, args in page_names:
            with self.subTest(page=name):
                url = reverse(name, args=args) if args else reverse(name)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302, f'{name} 未登录应跳登录')
                self.assertIn('login', response['Location'].lower())
        api_calls = [
            ('get', reverse('quote:api_options'), None),
            ('post', reverse('quote:api_calc'), payload_for(self.rows[0])),
            ('post', reverse('quote:api_create'), payload_for(self.rows[0])),
            ('post', reverse('quote:api_status', args=['FQ-0000-0000']), {'action': 'submit'}),
        ]
        for method, url, data in api_calls:
            with self.subTest(api=url):
                if method == 'get':
                    response = self.client.get(url)
                else:
                    response = self.client.post(
                        url, data=json.dumps(data), content_type='application/json')
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()['error'], 'auth_required')

    def test_d2_groups_and_admin_permissions_seeded(self):
        for role in ROLE_GROUPS:
            self.assertTrue(Group.objects.filter(name=role).exists(), f'缺少角色组 {role}')
        finance = self.role_users['finance']
        sales = self.role_users['sales']
        admin = self.role_users['quote_admin']
        self.assertTrue(finance.has_perm('quote.add_materialprice'))
        self.assertTrue(finance.has_perm('quote.change_processrate'))
        self.assertFalse(sales.has_perm('quote.add_materialprice'))
        self.assertFalse(sales.has_perm('quote.change_materialprice'))
        self.assertTrue(admin.has_perm('quote.add_materialprice'))
        self.assertTrue(admin.has_perm('auth.view_user'))

    def test_d3_sales_calc_must_not_expose_cost_breakdown(self):
        response = self._calc(self.role_users['sales'])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertNotIn('costs', body, '销售角色在 /quote/calc/ 看到了成本明细')
        self.assertIn('price', body, '销售应能看到价格/毛利结论')
        self.assertIn('margin_pct', body['price'])

    def test_d4_cost_roles_can_see_cost_breakdown(self):
        for role in ('engineering', 'finance', 'sales_manager', 'quote_admin'):
            with self.subTest(role=role):
                response = self._calc(self.role_users[role])
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertIn('costs', body, f'{role} 应能看成本明细')
                self.assertGreater(body['costs']['total'], 0)

    def test_d5_only_creators_can_create_quote(self):
        payload = payload_for(self.rows[0])
        for role in ('engineering', 'finance'):
            with self.subTest(role=role):
                self.client.force_login(self.role_users[role])
                response = self.client.post(
                    reverse('quote:api_create'),
                    data=json.dumps(payload), content_type='application/json')
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()['error'], 'permission_denied')
        for role in ('sales', 'sales_manager', 'quote_admin'):
            with self.subTest(role=role):
                self.client.force_login(self.role_users[role])
                response = self.client.post(
                    reverse('quote:api_create'),
                    data=json.dumps(payload), content_type='application/json')
                self.assertEqual(response.status_code, 200, f'{role} 应能创建报价单')
                self.assertTrue(response.json()['ok'])

    def test_d6_sales_list_only_sees_own_quotes(self):
        own = services.create_quote(payload_for(self.rows[0]), self.role_users['sales'])
        other = services.create_quote(payload_for(self.rows[1]), self.role_users['sales_manager'])
        self.client.force_login(self.role_users['sales'])
        response = self.client.get(reverse('quote:list'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn(own.number, html, '销售应能看到本人报价')
        self.assertNotIn(other.number, html, '销售看到了他人报价（数据范围越权）')

    def test_d7_sales_cannot_view_other_quote_detail(self):
        own = services.create_quote(payload_for(self.rows[0]), self.role_users['sales'])
        other = services.create_quote(payload_for(self.rows[1]), self.role_users['sales_manager'])
        self.client.force_login(self.role_users['sales'])
        self.assertEqual(self.client.get(
            reverse('quote:detail', args=[own.number])).status_code, 200)
        response = self.client.get(reverse('quote:detail', args=[other.number]))
        self.assertIn(response.status_code, (403, 404),
                      '销售可以打开他人报价详情（数据范围越权）')

    def test_d8_approval_requires_manager(self):
        pending = services.create_quote(
            payload_for(self.rows[0], discount_pct=10), self.role_users['sales'])
        services.transition(pending, 'submit', self.role_users['sales'])
        self.assertEqual(pending.status, Quotation.Status.PENDING)
        self.client.force_login(self.role_users['sales'])
        response = self.client.post(
            reverse('quote:api_status', args=[pending.number]),
            data=json.dumps({'action': 'approve'}), content_type='application/json')
        self.assertEqual(response.status_code, 403, '销售不应能审批报价')
        self.client.force_login(self.role_users['sales_manager'])
        response = self.client.post(
            reverse('quote:api_status', args=[pending.number]),
            data=json.dumps({'action': 'approve', 'comment': 'QA 验收'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], Quotation.Status.APPROVED)

    def test_d9_engineering_finance_cannot_transition(self):
        quote = services.create_quote(payload_for(self.rows[0]), self.role_users['sales'])
        for role in ('engineering', 'finance'):
            with self.subTest(role=role):
                self.client.force_login(self.role_users[role])
                response = self.client.post(
                    reverse('quote:api_status', args=[quote.number]),
                    data=json.dumps({'action': 'submit'}), content_type='application/json')
                self.assertEqual(response.status_code, 403)

    def test_d10_price_library_admin_write_permissions(self):
        self.client.force_login(self.staff_users['sales'])
        response = self.client.get(reverse('admin:quote_materialprice_changelist'))
        self.assertEqual(response.status_code, 403, '销售可进入材料价后台（越权）')
        self.client.force_login(self.staff_users['finance'])
        response = self.client.get(reverse('admin:quote_materialprice_changelist'))
        self.assertEqual(response.status_code, 200, '财务应能进入材料价后台')
        self.client.force_login(self.staff_users['quote_admin'])
        response = self.client.get(reverse('admin:quote_materialprice_changelist'))
        self.assertEqual(response.status_code, 200, '管理员应能进入材料价后台')

    def test_d11_admin_requires_staff(self):
        self.client.logout()
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'].lower())
        self.client.force_login(self.role_users['sales'])  # 非 staff
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.staff_users['quote_admin'])
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 200)

    def test_d12_sales_detail_hides_cost_values(self):
        quote = services.create_quote(payload_for(self.rows[0]), self.role_users['sales'])
        self.client.force_login(self.role_users['sales'])
        html = self.client.get(reverse('quote:detail', args=[quote.number])).content.decode()
        self.assertNotIn('单件成本合计', html, '销售详情页泄露了成本明细')
        self.assertNotIn('unit_cost', html)


# ============================================================================
# E. 状态机 / 版本 / 快照
# ============================================================================
class StateMachineTests(AcceptanceDataMixin, TestCase):
    def setUp(self):
        self.row = self.rows[0]

    def _quote(self, **overrides):
        return services.create_quote(payload_for(self.row, **overrides), self.user)

    def test_e1_full_chain_auto_approve(self):
        quote = self._quote()
        services.transition(quote, 'submit', self.user)
        self.assertEqual(quote.status, Quotation.Status.APPROVED, '无红线时应自动批准')
        services.transition(quote, 'send', self.user)
        self.assertEqual(quote.status, Quotation.Status.SENT)
        services.transition(quote, 'accept', self.user)
        self.assertEqual(quote.status, Quotation.Status.ACCEPTED)
        services.transition(quote, 'order', self.user)
        self.assertEqual(quote.status, Quotation.Status.ORDERED)
        actions = list(quote.approval_logs.values_list('action', flat=True))
        self.assertEqual(actions, ['submit', 'send', 'accept', 'order'])
        for log in quote.approval_logs.all():
            self.assertEqual(log.actor_id, self.user.pk)

    def test_e2_approval_chain_and_red_lines(self):
        cases = {
            'discount': {'discount_pct': 6},
            'total': {'qty': 1000},
            'margin': {'discount_pct': 10},
        }
        for name, overrides in cases.items():
            with self.subTest(red_line=name):
                quote = self._quote(**overrides)
                self.assertTrue(services.approval_required(quote),
                                f'{name} 未触发审批红线')
                services.transition(quote, 'submit', self.user)
                self.assertEqual(quote.status, Quotation.Status.PENDING)
                services.transition(quote, 'approve', self.user, comment='同意')
                self.assertEqual(quote.status, Quotation.Status.APPROVED)
                self.assertEqual(quote.approved_by_id, self.user.pk)

    def test_e3_reject_and_cancel_paths(self):
        pending = self._quote(discount_pct=6)
        services.transition(pending, 'submit', self.user)
        services.transition(pending, 'reject', self.user, comment='价格过高')
        self.assertEqual(pending.status, Quotation.Status.DRAFT)
        self.assertTrue(pending.approval_logs.filter(action='reject').exists())

        sent = self._quote()
        services.transition(sent, 'submit', self.user)
        services.transition(sent, 'send', self.user)
        services.transition(sent, 'reject', self.user)
        self.assertEqual(sent.status, Quotation.Status.REJECTED)

        # rejected 后可复制为新报价（新编号 V1 草稿）
        copied = services.copy_quote(sent, self.user)
        self.assertNotEqual(copied.number, sent.number)
        self.assertEqual(copied.version, 1)
        self.assertEqual(copied.status, Quotation.Status.DRAFT)
        self.assertEqual(copied.items.count(), sent.items.count())

        for status in (Quotation.Status.DRAFT, Quotation.Status.PENDING,
                       Quotation.Status.APPROVED, Quotation.Status.SENT,
                       Quotation.Status.ACCEPTED, Quotation.Status.REJECTED,
                       Quotation.Status.EXPIRED):
            quote = self._quote(**({'discount_pct': 6} if status == Quotation.Status.PENDING else {}))
            quote.status = status
            quote.save(update_fields=['status'])
            services.transition(quote, 'cancel', self.user)
            self.assertEqual(quote.status, Quotation.Status.CANCELLED, f'{status} 不能作废')

    def test_e4_expire_paths(self):
        approved = self._quote()
        services.transition(approved, 'submit', self.user)
        services.transition(approved, 'expire', self.user)
        self.assertEqual(approved.status, Quotation.Status.EXPIRED)
        sent = self._quote()
        services.transition(sent, 'submit', self.user)
        services.transition(sent, 'send', self.user)
        services.transition(sent, 'expire', self.user)
        self.assertEqual(sent.status, Quotation.Status.EXPIRED)

    def test_e5_invalid_transitions_are_rejected(self):
        quote = self._quote()
        invalid = [
            ('order', 409), ('approve', 409), ('send', 409), ('accept', 409),
        ]
        for action, status in invalid:
            with self.subTest(action=action):
                with self.assertRaises(services.InvalidTransition) as ctx:
                    services.transition(quote, action, self.user)
                self.assertEqual(ctx.exception.http_status, status)
        with self.assertRaises(services.InvalidPayload):
            services.transition(quote, 'nonsense', self.user)

    def test_e6_api_status_and_invalid_transition_contract(self):
        self.login()
        quote = self._quote()
        url = reverse('quote:api_status', args=[quote.number])
        response = self.client.post(
            url, data=json.dumps({'action': 'order'}), content_type='application/json')
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body['error'], 'invalid_transition')
        response = self.client.post(
            url, data=json.dumps({'action': 'submit'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], Quotation.Status.APPROVED)
        response = self.client.post(
            reverse('quote:api_status', args=['FQ-0000-0000']),
            data=json.dumps({'action': 'submit'}), content_type='application/json')
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            url, data=json.dumps({}), content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_e7_version_creation_archives_old_snapshot(self):
        quote = self._quote()
        services.transition(quote, 'submit', self.user)
        services.transition(quote, 'send', self.user)
        original_total = quote.total
        original_snapshot_id = quote.snapshot_id
        new_quote = services.create_version(quote, None, self.user, comment='客户改数量')
        self.assertEqual(new_quote.version, 2)
        self.assertNotEqual(new_quote.number, quote.number)
        self.assertEqual(new_quote.status, Quotation.Status.DRAFT)
        self.assertEqual(quote.status, Quotation.Status.CANCELLED)
        quote.refresh_from_db()
        self.assertEqual(quote.snapshot_id, original_snapshot_id)
        self.assertEqual(quote.total, original_total, '旧版本金额被改写')
        self.assertTrue(ApprovalLog.objects.filter(
            quotation=quote, action=ApprovalLog.Action.CANCEL).exists())
        self.assertTrue(ApprovalLog.objects.filter(
            quotation=new_quote, action=ApprovalLog.Action.REVISE).exists())
        self.assertEqual(new_quote.items.count(), quote.items.count())
        self.assertNotEqual(new_quote.snapshot_id, original_snapshot_id,
                            '新版本应生成独立快照')

    def test_e8_snapshot_is_frozen(self):
        quote = self._quote()
        snapshot = quote.snapshot
        self.assertEqual(snapshot.weight_rule, 'w0.3')
        self.assertEqual(snapshot.tax_rate, Decimal('13.00'))
        self.assertEqual(snapshot.fx_rate, Decimal('7.1000'))
        self.assertEqual(snapshot.created_by_id, self.user.pk)
        self.assertIsInstance(snapshot.params, dict)
        self.assertIn('rate_versions', snapshot.params)
        self.assertIsInstance(snapshot.standard_editions, dict)
        self.assertIsInstance(snapshot.price_versions, dict)
        # 快照不随后续价格库变化
        params_copy = json.loads(json.dumps(snapshot.params))
        MaterialPrice.objects.create(
            material=quote.items.first().material, price_per_kg=Decimal('88.88'),
            effective_from=date(2027, 1, 1), source='manual')
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.params, params_copy)


# ============================================================================
# F. 页面 / A4 / PDF / CSRF
# ============================================================================
class PagePrintPdfTests(AcceptanceDataMixin, TestCase):
    def setUp(self):
        self.login()
        self.quote = services.create_quote(payload_for(self.rows[0]), self.user)

    def test_f1_workbench_renders_full_catalog(self):
        response = self.client.get(reverse('quote:index'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('报价工作台', html)
        import re
        match = re.search(
            r'<script id="quote-bootstrap" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(match, '工作台未注入 #quote-bootstrap')
        bootstrap = json.loads(match.group(1))
        self.assertEqual(len(bootstrap['rows']), FULL_CATALOG_COUNT,
                         '工作台级联数据未覆盖 1,348 条规格')
        self.assertEqual(len(bootstrap['standards']), 5)
        self.assertTrue(bootstrap['csrf_token'])
        self.assertIn('calc', bootstrap['endpoints'])

    def test_f2_list_and_detail(self):
        response = self.client.get(reverse('quote:list'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn(self.quote.number, html)
        self.assertIn('报价单列表', html)

        response = self.client.get(reverse('quote:detail', args=[self.quote.number]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn(self.quote.number, html)
        self.assertIn('含税', html)
        self.assertNotIn('单件成本合计', html, '详情默认泄露成本（销售应不可见）')

    def test_f3_print_page_is_a4_document_only(self):
        response = self.client.get(reverse('quote:print', args=[self.quote.number]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('quote-print-body', html)
        self.assertIn('报 价 单', html)
        self.assertIn('no-print', html)
        self.assertIn('quote.css', html)
        self.assertNotIn('site-header', html, '打印页混入站点导航')
        self.assertNotIn('site-footer', html, '打印页混入站点页脚')
        # 打印样式：A4 纵向、隐藏工具栏、只输出单据
        css_path = BASE_DIR / 'static' / 'css' / 'quote.css'
        css = css_path.read_text('utf-8')
        self.assertIn('@page', css)
        self.assertIn('size: A4 portrait', css)
        self.assertIn('@media print', css)
        print_block = css.split('@media print', 1)[1]
        self.assertIn('display: none', print_block)
        # 打印页不得泄露成本明细
        self.assertNotIn('单件成本合计', html)
        # 金额与报价单一致
        self.assertIn(f'{self.quote.total:.2f}', html.replace(',', ''))

    def test_f4_print_token_for_pdf_renderer(self):
        from quote.pdf import print_page_token, verify_print_page_token
        token = print_page_token(self.quote.number)
        self.assertTrue(verify_print_page_token(token, self.quote.number))
        self.assertFalse(verify_print_page_token(token, 'FQ-0000-0000'))
        self.client.logout()
        response = self.client.get(
            reverse('quote:print', args=[self.quote.number]), {'pdf_token': token})
        self.assertEqual(response.status_code, 200)
        response = self.client.get(
            reverse('quote:print', args=[self.quote.number]), {'pdf_token': 'bad-token'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'].lower())

    def test_f5_pdf_degrades_gracefully_when_playwright_missing(self):
        playwright_available = importlib.util.find_spec('playwright') is not None
        response = self.client.get(reverse('quote:pdf', args=[self.quote.number]))
        if playwright_available:
            # 环境有 Playwright：要求真实出 PDF
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'application/pdf')
            payload = b''.join(response.streaming_content)
            self.assertTrue(payload.startswith(b'%PDF-'))
            self.assertGreater(len(payload), 1000)
        else:
            self.assertEqual(response.status_code, 503,
                             'Playwright 缺失时应降级 503，而不是 500/白屏')
            html = response.content.decode('utf-8')
            self.assertIn('PDF 生成暂不可用', html)
            self.assertIn('A4 打印页', html)

    def test_f6_pdf_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('quote:pdf', args=[self.quote.number]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'].lower())

    def test_f7_csrf_protected_post(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        payload = payload_for(self.rows[0], qty=1)
        response = csrf_client.post(
            reverse('quote:api_create'),
            data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 403, '无 CSRF token 的创建请求未被拦截')
        # 从工作台获取 token，再带 token 提交
        csrf_client.get(reverse('quote:index'))
        token = csrf_client.cookies['csrftoken'].value
        response = csrf_client.post(
            reverse('quote:api_create'),
            data=json.dumps(payload), content_type='application/json',
            HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, '携带 CSRF token 的创建请求失败')

    def test_f8_customers_and_prices_pages(self):
        response = self.client.get(reverse('quote:customers'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('demo-a', html)
        self.assertIn('默认折扣', html)
        response = self.client.get(reverse('quote:prices'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('价格库', html)
        self.assertIn('A105', html)
        self.assertIn('7.20', html)
        self.assertIn('演示', html, '演示价格未标注')

    def test_f9_list_filters_by_status(self):
        sent = services.create_quote(payload_for(self.rows[1]), self.user)
        services.transition(sent, 'submit', self.user)
        if sent.status == Quotation.Status.PENDING:
            services.transition(sent, 'approve', self.user)
        services.transition(sent, 'send', self.user)
        response = self.client.get(reverse('quote:list'), {'status': 'sent'})
        html = response.content.decode('utf-8')
        self.assertIn(sent.number, html)
        response = self.client.get(reverse('quote:list'), {'status': 'draft'})
        html = response.content.decode('utf-8')
        self.assertNotIn(sent.number, html)

    def test_f10_usd_quote_fx_display(self):
        """设计稿要求：外币报价按报价日汇率折算展示，并在单据注明汇率基准。"""
        usd_quote = services.create_quote(
            payload_for(self.rows[0], currency='USD', fx_rate=7.0), self.user)
        self.assertEqual(usd_quote.currency, 'USD')
        expected_unit_usd = (usd_quote.items.first().unit_price / usd_quote.fx_rate).quantize(
            MONEY_Q, rounding=ROUND_HALF_UP)
        expected_total_usd = (usd_quote.total / usd_quote.fx_rate).quantize(
            MONEY_Q, rounding=ROUND_HALF_UP)
        response = self.client.get(reverse('quote:detail', args=[usd_quote.number]))
        html = response.content.decode('utf-8').replace(',', '')
        self.assertIn('US$', html)
        self.assertIn(f'{expected_total_usd:.2f}', html,
                      'USD 报价未按 fx_rate 折算（直接显示了 CNY 金额）')
        self.assertIn(f'{expected_unit_usd:.2f}', html)


# ============================================================================
# G. 安全配置
# ============================================================================
class SecurityConfigTests(SimpleTestCase):
    def _run(self, env_extra):
        env = dict(os.environ)
        for key in ('DJANGO_DEBUG', 'DJANGO_SECRET_KEY', 'DJANGO_ALLOWED_HOSTS',
                    'DJANGO_HTTPS', 'DJANGO_CSRF_TRUSTED_ORIGINS'):
            env.pop(key, None)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, 'manage.py', 'check', '--deploy'],
            cwd=str(BASE_DIR), env=env, capture_output=True, text=True, timeout=120,
        )

    def test_g1_default_dev_settings_flagged_insecure(self):
        result = self._run({})
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0)
        self.assertIn('security.W018', output, 'DEBUG 开发默认值未被标记')
        self.assertIn('security.W009', output, '默认 SECRET_KEY 未被标记不安全')

    def test_g2_hardened_production_env_passes_deploy_check(self):
        result = self._run({
            'DJANGO_DEBUG': '0',
            'DJANGO_SECRET_KEY': 'qa-acceptance-' + hashlib.sha256(b'qa').hexdigest(),
            'DJANGO_ALLOWED_HOSTS': 'std.example.com,127.0.0.1',
            'DJANGO_CSRF_TRUSTED_ORIGINS': 'https://std.example.com',
            'DJANGO_HTTPS': '1',
        })
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0)
        for code in ('security.W009', 'security.W018', 'security.W012', 'security.W016'):
            self.assertNotIn(code, output, f'生产环境仍有 {code}')
        # 剩余告警只允许反向代理可兜底的项：HSTS 子域/preload、SSL 跳转
        for code in ('security.W005', 'security.W008', 'security.W021'):
            self.assertIn(code, output)
        self.assertLessEqual(output.count('security.'), 3)

    def test_g3_debug_off_and_env_overrides(self):
        env = dict(os.environ)
        env.update({
            'DJANGO_DEBUG': '0',
            'DJANGO_SECRET_KEY': 'production-secret-for-qa-only-0123456789abcdef',
            'DJANGO_ALLOWED_HOSTS': 'a.example.com, b.example.com',
        })
        code = (
            'import os, django; django.setup(); '
            'from django.conf import settings; '
            'print(settings.DEBUG); print(settings.ALLOWED_HOSTS); '
            "print(settings.SECRET_KEY.startswith('django-insecure-'))"
        )
        result = subprocess.run(
            [sys.executable, '-c', code], cwd=str(BASE_DIR), env=env,
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[0], 'False', 'DJANGO_DEBUG=0 未生效')
        self.assertEqual(lines[1], "['a.example.com', 'b.example.com']")
        self.assertEqual(lines[2], 'False', 'DJANGO_SECRET_KEY 环境变量未生效')

    def test_g4_deploy_script_backs_up_before_migrate(self):
        """上线清单要求：迁移前备份。deploy.sh 必须先备份再 migrate。"""
        script = (BASE_DIR / 'deploy' / 'deploy.sh').read_text('utf-8')
        self.assertIn('migrate', script)
        self.assertIn('backup', script.lower(),
                      'deploy.sh 迁移前没有备份步骤（上线阻塞项）')
        self.assertLess(script.lower().index('backup'), script.index('migrate'),
                        'deploy.sh 备份步骤在 migrate 之后')


# ============================================================================
# H. 性能
# ============================================================================
class PerformanceSmokeTests(AcceptanceDataMixin, TestCase):
    def test_h1_workbench_response_under_1s(self):
        self.login()
        # 预热
        self.client.get(reverse('quote:index'))
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            response = self.client.get(reverse('quote:index'))
            samples.append(time.perf_counter() - started)
            self.assertEqual(response.status_code, 200)
        best = min(samples)
        self.assertLess(best, 1.0, f'工作台首屏 {best * 1000:.0f}ms 超过 1s')

    def test_h2_single_calculation_under_50ms(self):
        payload = payload_for(self.rows[0])
        services.calculate(payload)  # 预热
        samples = []
        for _ in range(20):
            started = time.perf_counter()
            services.calculate(payload)
            samples.append(time.perf_counter() - started)
        median = sorted(samples)[len(samples) // 2]
        self.assertLess(median, 0.05, f'单次计算中位数 {median * 1000:.1f}ms 超过 50ms')

    def test_h3_full_catalog_options_and_calc_smoke(self):
        started = time.perf_counter()
        options = services.catalog_options()
        options_elapsed = time.perf_counter() - started
        self.assertLess(options_elapsed, 1.0, f'全量选项 {options_elapsed:.2f}s 超过 1s')
        self.assertEqual(
            sum(len(sizes) for std in options['catalog'].values()
                for ratings in std.values() for sizes in ratings.values()),
            FULL_CATALOG_COUNT,
        )
        started = time.perf_counter()
        count = 0
        for row in self.rows:
            services.calculate(payload_for(row, on_date=ON_DATE))
            count += 1
        elapsed = time.perf_counter() - started
        self.assertEqual(count, FULL_CATALOG_COUNT)
        self.assertLess(elapsed, 30.0, f'1,348 条全量计算 {elapsed:.1f}s 超过 30s')

    def test_h4_sqlite_concurrency_note(self):
        """验收说明：默认 SQLite 无 WAL/无行级锁；10 并发写会锁库。

        本测试只断言当前部署仍是 SQLite 并记录风险，真实并发冒烟见
        scripts/acceptance/perf_smoke.py（读写混合）。
        """
        engine = settings.DATABASES['default']['ENGINE']
        self.assertIn('sqlite3', engine)
        # 生产 DoD：要么改用 PostgreSQL，要么证明 10 并发不锁死（见报告）


# ============================================================================
# I. 备份 / 恢复
# ============================================================================
class BackupRestoreTests(SimpleTestCase):
    def _make_source_db(self, path: Path):
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                """
                CREATE TABLE quote_quotation (id INTEGER PRIMARY KEY, number TEXT);
                CREATE TABLE catalog_dimensionrow (id INTEGER PRIMARY KEY, size TEXT);
                CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, name TEXT);
                INSERT INTO quote_quotation (number) VALUES ('FQ-2611-0001'), ('FQ-2611-0002');
                INSERT INTO catalog_dimensionrow (size) VALUES ('4'), ('6'), ('8');
                INSERT INTO django_migrations (name) VALUES ('0001_initial'), ('0002_x');
                """
            )
            connection.commit()
        finally:
            connection.close()

    def test_i1_backup_restore_drill_script(self):
        script = QUOTE_SCRIPT_DIR / 'backup_restore_drill.py'
        self.assertTrue(script.exists(), '缺少备份/恢复演练脚本')
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / 'db.sqlite3'
            backup_dir = tmp_path / 'backups'
            pdf_dir = tmp_path / 'pdf'
            pdf_dir.mkdir()
            (pdf_dir / 'FQ-2611-0001.pdf').write_bytes(b'%PDF-1.4\n' + b'x' * 2000)
            (pdf_dir / 'FQ-2611-0002.pdf').write_bytes(b'%PDF-1.4\n' + b'y' * 2000)
            self._make_source_db(source)
            result = subprocess.run(
                [sys.executable, str(script), '--source', str(source),
                 '--backup-dir', str(backup_dir), '--pdf-dir', str(pdf_dir), '--json'],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload['ok'], payload)
            names = {check['name'] for check in payload['checks']}
            self.assertIn('integrity_check', names)
            self.assertIn('restore_counts_match', names)
            self.assertIn('pdf_backup', names)
            self.assertTrue(payload['database']['backup_exists'])
            self.assertEqual(payload['database']['counts']['quote_quotation'], 2)
            self.assertEqual(payload['pdf']['files_backed_up'], 2)

    def test_i2_deploy_backup_script_uses_sqlite_backup_api(self):
        script = (BASE_DIR / 'deploy' / 'backup.py').read_text('utf-8')
        self.assertIn('source.backup(dest)', script,
                      'deploy/backup.py 未使用 sqlite3 在线备份 API')
        self.assertIn('KEEP', script)
        self.assertIn('backups', script)

    def test_i3_backup_docs_cover_db_and_restore(self):
        guide = (BASE_DIR / 'docs' / '部署指南.md').read_text('utf-8')
        self.assertIn('备份', guide)
        self.assertIn('恢复', guide)
        self.assertIn('crontab', guide)
        self.assertIn('sqlite3', guide)

    def test_i4_pdf_backup_is_documented(self):
        guide = (BASE_DIR / 'docs' / '部署指南.md').read_text('utf-8')
        self.assertIn('PDF', guide, '部署指南未说明 PDF 缓存备份')
        self.assertIn('PDF_CACHE_DIR', guide)


# ============================================================================
# J. 部署门（记录性检查，结果写入验收报告）
# ============================================================================
class DeploymentGateTests(SimpleTestCase):
    def test_j1_requirements_pin_playwright(self):
        requirements = (BASE_DIR / 'requirements.txt').read_text('utf-8')
        self.assertIn('playwright', requirements)

    def test_j2_env_example_has_production_security_keys(self):
        env_example = (BASE_DIR / 'deploy' / 'env.example').read_text('utf-8')
        for key in ('DJANGO_DEBUG=0', 'DJANGO_SECRET_KEY', 'DJANGO_ALLOWED_HOSTS',
                    'DJANGO_CSRF_TRUSTED_ORIGINS', 'PDF_CACHE_DIR'):
            self.assertIn(key, env_example)

    def test_j3_health_endpoint_exists(self):
        self.assertTrue((BASE_DIR / 'catalog' / 'views.py').exists())
        from catalog import views as catalog_views
        self.assertTrue(callable(getattr(catalog_views, 'healthz', None)))

    def test_j4_demo_prices_are_marked_for_production_replacement(self):
        seed = (BASE_DIR / 'quote' / 'management' / 'commands' / 'seed_demo.py').read_text('utf-8')
        self.assertIn('禁止', seed)
        self.assertIn('demo', seed.lower())

    def test_j5_csv_and_excel_export_implemented(self):
        """DoD CSV/Excel：CSV 导出/导入与 T11 Excel（.xlsx）导出均已实现。"""
        js = (BASE_DIR / 'static' / 'js' / 'quote.js').read_text('utf-8')
        self.assertIn('text/csv', js)
        self.assertIn('exportCsv', js)
        command = BASE_DIR / 'quote' / 'management' / 'commands' / 'import_prices.py'
        self.assertTrue(command.exists())
        self.assertTrue(callable(getattr(prices, 'csv_template_text', None)))
        # T11：服务端 Excel 导出（openpyxl），不再是"延后/未实现"
        self.assertTrue((BASE_DIR / 'quote' / 'excel.py').exists())
        from quote import excel as excel_module

        self.assertTrue(callable(getattr(excel_module, 'build_quote_workbook', None)))
        self.assertTrue(callable(getattr(excel_module, 'build_quotes_workbook', None)))
        requirements = (BASE_DIR / 'requirements.txt').read_text('utf-8')
        self.assertIn('openpyxl', requirements)


# ============================================================================
# K. Excel 导出（T11 实现 · T12 验收更新）
# ============================================================================
XLSX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
COST_SECTION_LABEL = '单件成本明细（CNY，仅授权角色可见）'


class ExcelExportAcceptanceTests(AcceptanceDataMixin, TestCase):
    """K：Excel 导出的入口/内容/金额/权限/列表筛选/文件有效性。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command('seed_roles', verbosity=0)
        cls.contract_row = DimensionRow.objects.get(
            standard_id='asme-b16-5', flange_type_id='weld-neck',
            size='4', rating__code='150')
        cls.roles = {}
        for role in ROLE_GROUPS:
            user = User.objects.create_user(f'qa-excel-{role}', password='qa-pass-12345')
            user.groups.add(Group.objects.get(name=role))
            cls.roles[role] = user

    # -- helpers -----------------------------------------------------------
    def _quote(self, user=None, **overrides):
        return services.create_quote(
            payload_for(self.contract_row, **overrides),
            user or self.roles['sales'],
        )

    def _load(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], XLSX_CONTENT_TYPE)
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn('.xlsx', response['Content-Disposition'])
        return load_workbook(BytesIO(response.content))

    @staticmethod
    def _texts(workbook):
        return [
            str(cell.value)
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if cell.value is not None
        ]

    @staticmethod
    def _numbers(workbook):
        return [
            float(cell.value)
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool)
        ]

    # -- tests -------------------------------------------------------------
    def test_k1_single_excel_content_type_and_amounts(self):
        quote = self._quote()
        self.client.force_login(self.roles['sales'])
        response = self.client.get(reverse('quote:excel', args=[quote.number]))
        workbook = self._load(response)
        self.assertIn(quote.number, response['Content-Disposition'])
        self.assertIn(quote.number, workbook.sheetnames)
        texts = self._texts(workbook)
        numbers = self._numbers(workbook)
        self.assertTrue(any('报 价 单' in text for text in texts))
        self.assertTrue(any('数据来源' in text for text in texts))
        self.assertTrue(any(quote.number in text for text in texts))
        self.assertTrue(any('B16.5' in text and '对焊' in text for text in texts))
        # 金额/明细与报价单一致
        self.assertIn(float(quote.total), numbers)
        self.assertIn(float(quote.subtotal), numbers)
        self.assertIn(float(quote.tax_amount), numbers)
        item = quote.items.first()
        self.assertIn(float(item.unit_price), numbers)
        self.assertIn(float(item.amount), numbers)
        self.assertIn(float(item.qty), numbers)
        self.assertIn(float(item.net_weight), numbers)
        # 打印版式：A4 横向、打印区域
        worksheet = workbook[quote.number]
        self.assertTrue(worksheet.print_area)
        self.assertEqual(worksheet.page_setup.orientation, 'landscape')
        self.assertEqual(str(worksheet.page_setup.paperSize), '9')  # A4
        # 销售未授权：无成本区块
        self.assertFalse(any('单件成本合计' in text for text in texts))

    def test_k2_excel_cost_block_only_for_cost_roles(self):
        quote = self._quote()
        for role in ('engineering', 'finance', 'sales_manager', 'quote_admin'):
            with self.subTest(role=role):
                self.client.force_login(self.roles[role])
                response = self.client.get(reverse('quote:excel', args=[quote.number]))
                texts = self._texts(self._load(response))
                self.assertTrue(any(COST_SECTION_LABEL in text for text in texts),
                                f'{role} 未导出成本区块')
                self.assertTrue(any('单件成本合计' in text for text in texts))
        self.client.force_login(self.roles['sales'])
        texts = self._texts(self._load(
            self.client.get(reverse('quote:excel', args=[quote.number]))))
        self.assertFalse(any(COST_SECTION_LABEL in text for text in texts))
        self.assertFalse(any('单件成本合计' in text for text in texts))

    def test_k3_excel_requires_login_and_respects_scope(self):
        quote = self._quote()
        self.client.logout()
        for url in (reverse('quote:excel', args=[quote.number]), reverse('quote:export')):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn('login', response['Location'].lower())
        # sales 仅能导出本人；他人 403
        other = self._quote(user=self.roles['sales_manager'])
        self.client.force_login(self.roles['sales'])
        self.assertEqual(
            self.client.get(reverse('quote:excel', args=[quote.number])).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('quote:excel', args=[other.number])).status_code, 403)
        # 成本角色可导出他人
        self.client.force_login(self.roles['engineering'])
        self.assertEqual(
            self.client.get(reverse('quote:excel', args=[quote.number])).status_code, 200)
        # 未知单号 404
        self.client.force_login(self.roles['sales_manager'])
        self.assertEqual(
            self.client.get(reverse('quote:excel', args=['FQ-0000-0000'])).status_code, 404)

    def test_k4_list_export_filter_and_data_scope(self):
        draft = self._quote()
        approved = self._quote()
        services.transition(approved, 'submit', self.roles['sales'])  # 无红线 → 自动批准
        approved.refresh_from_db()
        self.assertEqual(approved.status, Quotation.Status.APPROVED)
        manager_quote = self._quote(user=self.roles['sales_manager'])

        self.client.force_login(self.roles['sales_manager'])
        response = self.client.get(reverse('quote:export') + '?status=draft')
        workbook = self._load(response)
        self.assertIn('报价单列表', workbook.sheetnames)
        self.assertIn(draft.number, workbook.sheetnames)
        self.assertNotIn(approved.number, workbook.sheetnames)
        self.assertIn(draft.number, self._texts(workbook))

        # 数据范围：sales 的列表导出不得含他人报价
        self.client.force_login(self.roles['sales'])
        workbook = self._load(self.client.get(reverse('quote:export')))
        self.assertIn(draft.number, workbook.sheetnames)
        self.assertIn(approved.number, workbook.sheetnames)
        self.assertNotIn(manager_quote.number, workbook.sheetnames)
        # 中文文件名通过 filename* 下发，且可被浏览器识别为 xlsx
        response = self.client.get(reverse('quote:export'))
        self.assertIn('.xlsx', response['Content-Disposition'])

    def test_k5_multi_item_workbook_valid_and_consistent(self):
        slip_on = DimensionRow.objects.filter(
            standard_id='asme-b16-5', flange_type_id='slip-on',
            size='4', rating__code='150').first()
        self.assertIsNotNone(slip_on)
        payload = {
            'items': [
                payload_for(self.contract_row, qty=10),
                payload_for(slip_on, qty=2),
            ],
            'on_date': ON_DATE,
            'valid_until': VALID_UNTIL,
        }
        quote = services.create_quote(payload, self.roles['sales'])
        self.assertEqual(quote.items.count(), 2)
        self.client.force_login(self.roles['sales'])
        workbook = self._load(self.client.get(reverse('quote:excel', args=[quote.number])))
        numbers = self._numbers(workbook)
        for item in quote.items.all():
            self.assertIn(float(item.amount), numbers)
            self.assertIn(float(item.unit_price), numbers)
        self.assertIn(float(quote.subtotal), numbers)
        self.assertIn(float(quote.tax_amount), numbers)
        self.assertIn(float(quote.total), numbers)
        # 金额必须是值而不是公式（财务可直接复核）
        formulas = [
            cell.coordinate
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if cell.data_type == 'f'
        ]
        self.assertFalse(formulas, f'Excel 含公式：{formulas[:5]}')
        # 重新保存再打开，证明 xlsx 结构有效、可被 Excel/openpyxl 读取
        buffer = BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        reopened = load_workbook(buffer)
        self.assertIn(quote.number, reopened.sheetnames)

    def test_k6_excel_entry_points_on_pages(self):
        quote = self._quote()
        self.client.force_login(self.roles['sales'])
        detail = self.client.get(reverse('quote:detail', args=[quote.number])).content.decode()
        listing = self.client.get(reverse('quote:list')).content.decode()
        self.assertIn(reverse('quote:excel', args=[quote.number]), detail)
        self.assertIn(reverse('quote:excel', args=[quote.number]), listing)
        self.assertIn(reverse('quote:export'), listing)


# ============================================================================
# L. 动态成本参数（T13–T15 实现 · T16 验收更新）
# ============================================================================
class DynamicCostParamAcceptanceTests(AcceptanceDataMixin, TestCase):
    """L：本次材料价/工资率覆盖、快照来源、权限、详情/A4/Excel 展示、历史不变。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command('seed_roles', verbosity=0)
        cls.contract_row = DimensionRow.objects.get(
            standard_id='asme-b16-5', flange_type_id='weld-neck',
            size='4', rating__code='150')
        cls.roles = {}
        for role in ROLE_GROUPS:
            user = User.objects.create_user(f'qa-dyn-{role}', password='qa-pass-12345')
            user.groups.add(Group.objects.get(name=role))
            cls.roles[role] = user

    # -- helpers -----------------------------------------------------------
    def _payload(self, **dynamic_params):
        payload = payload_for(self.contract_row, on_date=ON_DATE)
        payload['params'] = {**payload['params'], **dynamic_params}
        return payload

    def _calc(self, user, **dynamic_params):
        return services.calculate(self._payload(**dynamic_params), user=user)

    @staticmethod
    def _excel_texts(response):
        workbook = load_workbook(BytesIO(response.content))
        return [
            str(cell.value)
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if cell.value is not None
        ]

    # -- tests -------------------------------------------------------------
    def test_l1_manual_material_price_changes_cost_price_snapshot(self):
        base = self._calc(self.roles['engineering'])
        manual = self._calc(self.roles['engineering'], material_price_per_kg=10)
        self.assertEqual(base['costs']['material'], 69.94)
        self.assertEqual(manual['costs']['material'], 97.14)
        self.assertGreater(manual['price']['unit_gross'], base['price']['unit_gross'])
        snap = manual['snapshot']['params']
        self.assertEqual(snap['material_price_per_kg'], 10.0)
        self.assertEqual(snap['material_price_base'], 7.2)
        self.assertEqual(snap['material_price_origin'], 'manual')
        self.assertEqual(snap['material_price_source'], 'manual')
        self.assertEqual(snap['material_price_library_source'], 'demo')
        self.assertFalse(snap['demo_material_price'])
        versions = manual['snapshot']['price_versions']['material_prices']['A105']
        self.assertEqual(versions['actual_price_per_kg'], 10.0)
        self.assertEqual(versions['price_per_kg'], 7.2)

    def test_l2_manual_machining_rate_changes_cost_and_rate_source(self):
        base = self._calc(self.roles['engineering'])
        manual = self._calc(self.roles['engineering'], machining_rate=50)
        self.assertEqual(base['costs']['machining'], 20.70)
        self.assertEqual(manual['costs']['machining'], 57.50)
        snap = manual['snapshot']['params']
        self.assertEqual(snap['machining_rate'], 50.0)
        self.assertEqual(snap['machining_rate_source'], 'manual')
        self.assertEqual(snap['rate_source'], 'manual_override')
        for key in ('demo_rate_codes', 'default_rate_codes', 'real_rate_codes'):
            self.assertNotIn('machining', snap[key], f'{key} 仍把 machining 当库价/默认费率')
        zero = self._calc(self.roles['engineering'], machining_rate=0)
        self.assertEqual(zero['costs']['machining'], 0.0)
        self.assertEqual(zero['snapshot']['params']['machining_rate_source'], 'manual')

    def test_l3_without_override_uses_library_price_and_process_rates(self):
        result = self._calc(self.roles['engineering'])
        snap = result['snapshot']['params']
        self.assertEqual(snap['material_price_per_kg'], 7.2)
        self.assertEqual(snap['material_price_base'], 7.2)
        self.assertEqual(snap['material_price_origin'], 'library')
        self.assertEqual(snap['material_price_source'], 'demo')
        self.assertEqual(snap['material_price_library_source'], 'demo')
        self.assertTrue(snap['demo_material_price'])
        # 价格库已有 machining 工艺费率 → 使用档位基价 18，不是手工覆盖
        self.assertEqual(snap['machining_rate'], 18.0)
        self.assertEqual(snap['machining_rate_source'], 'process')
        self.assertEqual(snap['rate_source'], 'demo_defaults')
        self.assertNotEqual(snap['material_price_origin'], 'manual')
        self.assertNotEqual(snap['machining_rate_source'], 'manual')

    def test_l4_sales_manual_override_forbidden_others_allowed(self):
        manual = self._payload(material_price_per_kg=10)
        with self.assertRaises(services.PermissionDenied) as ctx:
            services.calculate(manual, user=self.roles['sales'])
        self.assertEqual(ctx.exception.code, 'permission_denied')
        self.assertEqual(ctx.exception.http_status, 403)
        self.assertEqual(ctx.exception.field, 'params')

        self.client.force_login(self.roles['sales'])
        response = self.client.post(
            reverse('quote:api_calc'), data=json.dumps(manual),
            content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'permission_denied')
        # sales 即使能建单，也不能借创建接口注入手工成本
        response = self.client.post(
            reverse('quote:api_create'), data=json.dumps(manual),
            content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'permission_denied')

        self.client.force_login(self.roles['engineering'])
        response = self.client.post(
            reverse('quote:api_calc'),
            data=json.dumps(self._payload(machining_rate=50)),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['snapshot']['params']['rate_source'], 'manual_override')
        self.assertEqual(response.json()['costs']['machining'], 57.5)

        quote = services.create_quote(
            self._payload(material_price_per_kg=10, machining_rate=50),
            self.roles['sales_manager'])
        self.assertEqual(quote.snapshot.params['material_price_origin'], 'manual')
        self.assertEqual(quote.items.first().material_price, Decimal('10.00'))

    def test_l5_source_display_detail_print_excel_and_sales_hidden(self):
        manual_quote = services.create_quote(
            self._payload(material_price_per_kg=10, machining_rate=50),
            self.roles['sales_manager'])

        self.client.force_login(self.roles['engineering'])
        detail = self.client.get(
            reverse('quote:detail', args=[manual_quote.number])).content.decode('utf-8')
        for text in ('本次覆盖', '¥10.00', '¥50.00', '材料价来源', '工资率来源', '单件成本合计'):
            self.assertIn(text, detail, f'详情缺少来源/成本展示：{text}')

        print_page = self.client.get(
            reverse('quote:print', args=[manual_quote.number])).content.decode('utf-8')
        for text in ('本次覆盖', '10.00', '50.00'):
            self.assertIn(text, print_page, f'A4 打印页缺少来源展示：{text}')

        excel_response = self.client.get(reverse('quote:excel', args=[manual_quote.number]))
        self.assertEqual(excel_response.status_code, 200)
        excel_texts = self._excel_texts(excel_response)
        self.assertTrue(any('本次覆盖' in text for text in excel_texts))
        self.assertTrue(any('10.00' in text for text in excel_texts))
        self.assertTrue(any('50.00' in text for text in excel_texts))

        # 销售本人普通报价：详情/打印/Excel 均无成本来源区块
        sales_quote = services.create_quote(self._payload(), self.roles['sales'])
        self.client.force_login(self.roles['sales'])
        own_detail = self.client.get(
            reverse('quote:detail', args=[sales_quote.number])).content.decode('utf-8')
        self.assertNotIn('本次覆盖', own_detail)
        self.assertNotIn('材料价来源', own_detail)
        self.assertNotIn('单件成本合计', own_detail)
        own_excel = self._excel_texts(
            self.client.get(reverse('quote:excel', args=[sales_quote.number])))
        self.assertFalse(any('本次覆盖' in text for text in own_excel))
        self.assertFalse(any('单件成本合计' in text for text in own_excel))
        # 销售不能查看/导出/打印他人的手工覆盖报价
        self.assertEqual(self.client.get(
            reverse('quote:detail', args=[manual_quote.number])).status_code, 403)
        self.assertEqual(self.client.get(
            reverse('quote:excel', args=[manual_quote.number])).status_code, 403)
        self.assertEqual(self.client.get(
            reverse('quote:print', args=[manual_quote.number])).status_code, 403)

    def test_l6_history_snapshot_not_rewritten_by_library_or_new_manual(self):
        old_quote = services.create_quote(
            self._payload(material_price_per_kg=10), self.roles['sales_manager'])
        snapshot_id = old_quote.snapshot_id
        old_params = dict(old_quote.snapshot.params)
        old_total = old_quote.total
        old_item = old_quote.items.first()
        old_material_price = old_item.material_price
        old_costs = dict(old_item.cost_breakdown['costs'])

        library_price = MaterialPrice.objects.filter(
            material__code='A105').order_by('-effective_from', '-id').first()
        library_price.price_per_kg = Decimal('9.99')
        library_price.save(update_fields=['price_per_kg'])
        services.create_quote(
            self._payload(material_price_per_kg=11.5), self.roles['sales_manager'])

        old_quote.refresh_from_db()
        old_item.refresh_from_db()
        self.assertEqual(old_quote.snapshot_id, snapshot_id)
        self.assertEqual(old_quote.snapshot.params, old_params)
        self.assertEqual(old_quote.total, old_total)
        self.assertEqual(old_item.material_price, old_material_price)
        self.assertEqual(old_item.cost_breakdown['costs'], old_costs)

        replay = self._calc(self.roles['engineering'], material_price_per_kg=10)
        self.assertEqual(replay['snapshot']['params']['material_price_per_kg'], 10.0)
        self.assertEqual(replay['costs'], old_costs)
        # 不传手工价的新计算按新库价；历史单不随之改变
        library_now = self._calc(self.roles['engineering'])
        self.assertEqual(library_now['snapshot']['params']['material_price_per_kg'], 9.99)
        self.assertEqual(library_now['snapshot']['params']['material_price_origin'], 'library')

    def test_l7_manual_params_validation_and_missing_library_price(self):
        invalid = [
            ({'material_price_per_kg': 0}, 'material_price_per_kg'),
            ({'material_price_per_kg': -1}, 'material_price_per_kg'),
            ({'machining_rate': -1}, 'machining_rate'),
        ]
        for params, field in invalid:
            with self.subTest(params=params):
                with self.assertRaises(services.InvalidPayload) as ctx:
                    self._calc(self.roles['engineering'], **params)
                self.assertEqual(ctx.exception.field, field)
        self.client.force_login(self.roles['engineering'])
        response = self.client.post(
            reverse('quote:api_calc'),
            data=json.dumps(self._payload(material_price_per_kg=0)),
            content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'invalid_payload')
        self.assertEqual(response.json()['field'], 'material_price_per_kg')

        # 无价格库记录时，手工材料价可正常报价；不传则必须报 price_unavailable
        Material.objects.create(
            code='F304X', label='ASTM A182 F304', family='不锈钢 304',
            density=Decimal('7.930'))
        manual_payload = self._payload(material_price_per_kg=28)
        manual_payload['material'] = 'F304X'
        result = services.calculate(manual_payload, user=self.roles['engineering'])
        snap = result['snapshot']['params']
        self.assertEqual(snap['material_price_origin'], 'manual')
        self.assertIsNone(snap['material_price_base'])
        self.assertIsNone(snap['material_price_library_source'])
        self.assertGreater(result['costs']['material'], 0)
        no_manual = self._payload()
        no_manual['material'] = 'F304X'
        with self.assertRaises(services.PriceUnavailable):
            services.calculate(no_manual, user=self.roles['engineering'])
