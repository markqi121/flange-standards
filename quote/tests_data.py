"""T3 数据层测试：取价 / 客户折扣 / CSV 导入 / Admin / 演示种子。

运行：``.venv/bin/python manage.py test quote``

覆盖：

- 按报价日取最近生效材料价、费率档位、缺失价格报错；
- 客户默认折扣与额外折扣合并、0–30% / 0–50% 边界；
- CSV 表头别名（含中文）、逐行校验错误与行号、唯一键去重、模板列；
- ``import_prices`` 幂等、strict 整批回滚、dry-run、错误行报告；
- 四个模型的 admin 注册与列表/筛选/搜索/生效日期配置，demo 来源可选；
- ``seed_demo`` 23 材质价格与 HTML 原型一致、全部 source=demo、幂等、可清理、
  生产环境（DEBUG=False）默认拒绝、种子数据可被 ``services.calculate`` 使用。
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib import admin as django_admin
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings

from catalog.models import DimensionRow, FlangeType, Rating, Standard

from quote import admin as quote_admin  # noqa: F401  确保 admin 已注册
from quote import prices, services
from quote.management.commands import seed_demo
from quote.models import Customer, Material, MaterialPrice, ProcessRate

# seed_demo 期望值（与 docs/法兰报价系统设计方案.html 原型 §7 一致）
EXPECTED_DEMO_PRICES = {
    'A105': '7.20',
    'LF2': '8.60',
    'F304': '28.00',
    'F316L': '35.00',
    'F11': '10.20',
    'F22': '12.00',
    'F91': '16.00',
    'F51': '62.00',
    'A694F52': '10.50',
    'A694F60': '11.50',
    'P250GH': '7.00',
    'P265GH': '7.20',
    'S235JR': '6.60',
    'S355J2': '7.80',
    '1.4301': '27.00',
    '1.4404': '34.00',
    '1.4571': '38.00',
    'SS400': '6.60',
    'S25C': '7.20',
    'SUS304': '27.00',
    'SUS316': '34.00',
    'SCS13': '30.00',
    'SCS14': '38.00',
}


def make_csv(*rows) -> str:
    """把二维表格转成 CSV 文本（自动处理引号/逗号）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def run_command(command: str, **options):
    """执行管理命令并返回 (output, exception)。异常不抛出，便于断言。"""
    stdout = io.StringIO()
    try:
        call_command(command, stdout=stdout, stderr=io.StringIO(), **options)
    except CommandError as exc:
        return stdout.getvalue(), exc
    return stdout.getvalue(), None


# ================================================================ 取价
class PriceLookupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.material = Material.objects.create(
            code='A105', label='ASTM A105 / A105N', family='碳钢',
            density=Decimal('7.850'),
        )
        cls.price_old = MaterialPrice.objects.create(
            material=cls.material, price_per_kg=Decimal('5.00'),
            effective_from=date(2019, 1, 1), source='purchase',
        )
        cls.price = MaterialPrice.objects.create(
            material=cls.material, price_per_kg=Decimal('7.20'),
            effective_from=date(2020, 1, 1), source='purchase',
        )
        ProcessRate.objects.create(
            code='machining', label='加工费', unit='per_piece',
            rate=Decimal('200'), tiers={'50': 12, '100': 18, '200': 28},
            effective_from=date(2020, 1, 1), source='manual',
        )
        ProcessRate.objects.create(
            code='freight', label='运费', unit='per_kg',
            rate=Decimal('0.60'), effective_from=date(2020, 1, 1), source='manual',
        )

    def test_price_for_picks_latest_effective_and_boundary(self):
        self.assertEqual(prices.price_for(self.material, date(2019, 1, 1)), self.price_old)
        self.assertEqual(prices.price_for(self.material, date(2019, 6, 1)), self.price_old)
        self.assertEqual(prices.price_for(self.material, date(2020, 1, 1)), self.price)
        self.assertEqual(prices.price_for(self.material, date(2026, 11, 6)), self.price)

    def test_price_for_missing_raises(self):
        with self.assertRaises(prices.PriceError):
            prices.price_for(self.material, date(2018, 12, 31))

    def test_rate_for_uses_tiers_and_base(self):
        on_date = date(2026, 1, 1)
        self.assertEqual(prices.rate_for('machining', on_date, dn=100), Decimal('18'))
        self.assertEqual(prices.rate_for('machining', on_date, dn=200), Decimal('28'))
        # 超过最后一档 → 基础费率
        self.assertEqual(prices.rate_for('machining', on_date, dn=300), Decimal('200'))
        self.assertEqual(prices.rate_for('freight', on_date), Decimal('0.60'))
        with self.assertRaises(prices.PriceError):
            prices.rate_for('machining', date(2019, 1, 1))

    def test_material_prices_on_returns_effective_map(self):
        result = prices.material_prices_on(date(2026, 1, 1))
        self.assertEqual(result['A105'], self.price)


# ================================================================ 客户折扣
class CustomerDiscountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer_a = Customer.objects.create(
            code='demo-a', name='示例客户 A', level='A',
            discount_pct=Decimal('8.00'), currency='CNY',
        )
        cls.customer_max = Customer.objects.create(
            code='demo-x', name='示例客户 X', level='C',
            discount_pct=Decimal('30.00'), currency='CNY',
        )
        cls.customer_bad = Customer.objects.create(
            code='demo-bad', name='折扣越界客户', level='C',
            discount_pct=Decimal('40.00'), currency='CNY',
        )

    def test_customer_default_discount(self):
        self.assertEqual(prices.customer_discount_pct(None), Decimal('0'))
        self.assertEqual(prices.customer_discount_pct(self.customer_a), Decimal('8.00'))

    def test_resolve_discount_with_extra(self):
        self.assertEqual(prices.resolve_discount(self.customer_a), Decimal('8'))
        self.assertEqual(
            prices.resolve_discount(self.customer_a, Decimal('4')), Decimal('12')
        )
        # 合并上限 50%
        self.assertEqual(
            prices.resolve_discount(self.customer_max, Decimal('30')), Decimal('50')
        )

    def test_invalid_discount_raises(self):
        with self.assertRaises(prices.PriceError):
            prices.customer_discount_pct(self.customer_bad)
        with self.assertRaises(prices.PriceError):
            prices.resolve_discount(self.customer_a, Decimal('-1'))

    def test_discount_source_breakdown(self):
        data = prices.discount_source(self.customer_a, Decimal('2'))
        self.assertEqual(data['customer_code'], 'demo-a')
        self.assertEqual(data['customer_discount_pct'], Decimal('8.00'))
        self.assertEqual(data['extra_discount_pct'], Decimal('2.00'))
        self.assertEqual(data['total_discount_pct'], Decimal('10'))


# ================================================================ CSV 解析
class CSVParsingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.material = Material.objects.create(
            code='A105', label='ASTM A105 / A105N', family='碳钢',
            density=Decimal('7.850'),
        )

    def test_material_price_parse_with_chinese_headers(self):
        text = make_csv(
            ('材质代码', '单价', '币种', '生效日期', '来源', '备注'),
            ('A105', '7.20', 'CNY', '2026-01-01', '演示', '演示价'),
        )
        parsed, errors = prices.parse_csv_text('materials', text)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        row = parsed[0]
        self.assertEqual(row['material'], self.material)
        self.assertEqual(row['price_per_kg'], Decimal('7.20'))
        self.assertEqual(row['currency'], 'CNY')
        self.assertEqual(row['effective_from'], date(2026, 1, 1))
        self.assertEqual(row['source'], prices.DEMO_SOURCE)

    def test_material_price_row_errors_have_line_numbers(self):
        text = make_csv(
            ('material_code', 'price_per_kg', 'currency', 'effective_from', 'source'),
            ('A105', 'abc', 'CNY', '2026-01-01', 'manual'),      # 2 价格非法
            ('A105', '7.20', 'CNY', 'bad-date', 'manual'),       # 3 日期非法
            ('A105', '7.20', 'USD', '2026-01-01', 'manual'),     # 4 币种非法
            ('NOPE', '7.20', 'CNY', '2026-01-01', 'manual'),     # 5 材质不存在
            ('A105', '7.20', 'CNY', '2026-01-01', 'unknown'),    # 6 来源非法
        )
        parsed, errors = prices.parse_csv_text('materials', text)
        self.assertEqual(parsed, [])
        self.assertEqual([error.line for error in errors], [2, 3, 4, 5, 6])
        self.assertTrue(any('price_per_kg' in str(error) for error in errors))
        self.assertTrue(any('NOPE' in str(error) for error in errors))

    def test_material_price_duplicate_rows_reported(self):
        text = make_csv(
            ('material_code', 'price_per_kg', 'effective_from'),
            ('A105', '7.20', '2026-01-01'),
            ('A105', '8.00', '2026-01-01'),
        )
        parsed, errors = prices.parse_csv_text('materials', text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].line, 3)
        self.assertIn('重复', errors[0].message)

    def test_process_rate_parse_json_tiers(self):
        text = make_csv(
            ('code', 'label', 'unit', 'rate', 'tiers', 'effective_from'),
            # 契约示例写法 <=50 会被规范成 services 需要的数字档位
            ('machining', '加工费', '元/件', '200', '{"<=50": 12, "<=100": 18}', '2026-01-01'),
            ('freight', '运费', 'per_kg', '0.60', '', '2026-01-01'),
        )
        parsed, errors = prices.parse_csv_text('rates', text)
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0]['unit'], 'per_piece')
        self.assertEqual(parsed[0]['tiers'], {'50': 12, '100': 18})
        self.assertEqual(parsed[1]['tiers'], {})

    def test_machining_tiers_must_be_dn_limits(self):
        text = make_csv(
            ('code', 'unit', 'rate', 'tiers', 'effective_from'),
            ('machining', 'per_piece', '200', '{"abc": 1}', '2026-01-01'),
        )
        parsed, errors = prices.parse_csv_text('rates', text)
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn('DN', errors[0].message)

    def test_process_rate_errors(self):
        text = make_csv(
            ('code', 'unit', 'rate', 'tiers', 'effective_from'),
            ('machining', 'per_piece', '200', '{bad json}', '2026-01-01'),  # 2 档位
            ('surface', '元/个', '1.2', '', '2026-01-01'),                 # 3 单位
            ('freight', 'per_kg', '-1', '', '2026-01-01'),                  # 4 负费率
        )
        parsed, errors = prices.parse_csv_text('rates', text)
        self.assertEqual(parsed, [])
        self.assertEqual([error.line for error in errors], [2, 3, 4])

    def test_customer_parse_and_errors(self):
        good = make_csv(
            ('code', 'name', 'level', 'discount_pct', 'currency', 'payment_terms', 'active'),
            ('demo-a', '示例客户 A', 'a', '8', 'cny', '30% 预付', 'true'),
        )
        parsed, errors = prices.parse_csv_text('customers', good)
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0]['level'], 'A')
        self.assertEqual(parsed[0]['currency'], 'CNY')
        self.assertEqual(parsed[0]['discount_pct'], Decimal('8.00'))
        self.assertTrue(parsed[0]['active'])

        bad = make_csv(
            ('code', 'name', 'level', 'discount_pct', 'currency', 'active'),
            ('demo-b', 'B', 'D', '4', 'CNY', 'true'),
            ('demo-c', 'C', 'C', '4', 'EUR', 'true'),
            ('demo-d', 'D', 'C', '31', 'CNY', 'true'),
            ('demo-e', 'E', 'C', '4', 'CNY', 'maybe'),
        )
        parsed, errors = prices.parse_csv_text('customers', bad)
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 4)

    def test_missing_required_header_raises(self):
        with self.assertRaises(prices.CSVError):
            prices.parse_csv_text('materials', 'foo,bar\n1,2\n')
        with self.assertRaises(prices.CSVError):
            prices.parse_csv_text('unknown', 'a,b\n1,2\n')

    def test_templates_have_documented_columns(self):
        for kind, spec in prices.CSV_KINDS.items():
            text = prices.csv_template_text(kind)
            header = text.splitlines()[0]
            self.assertEqual(header, ','.join(spec['columns']))
        with self.assertRaises(prices.CSVError):
            prices.csv_template_text('unknown')

    def test_fixture_templates_match_command_template(self):
        base = Path(seed_demo.__file__).resolve().parents[2] / 'fixtures' / 'csv'
        for kind in prices.CSV_KINDS:
            path = base / f'{kind}.csv'
            self.assertTrue(path.exists(), f'缺少 CSV 模板：{path}')
            self.assertEqual(
                path.read_text(encoding='utf-8-sig'), prices.csv_template_text(kind)
            )


# ================================================================ 导入命令
class ImportCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.material = Material.objects.create(
            code='A105', label='ASTM A105 / A105N', family='碳钢',
            density=Decimal('7.850'),
        )

    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding='utf-8-sig')
        return path

    def test_import_materials_idempotent(self):
        path = self._write(
            'materials.csv',
            make_csv(
                ('material_code', 'price_per_kg', 'currency', 'effective_from', 'source', 'note'),
                ('A105', '7.20', 'CNY', '2026-01-01', 'demo', '演示价'),
            ),
        )
        output, error = run_command('import_prices', kind='materials', path=str(path))
        self.assertIsNone(error)
        self.assertIn('新增 1', output)
        self.assertEqual(MaterialPrice.objects.count(), 1)
        self.assertEqual(MaterialPrice.objects.get().source, 'demo')

        output, error = run_command('import_prices', kind='materials', path=str(path))
        self.assertIsNone(error)
        self.assertIn('更新 1', output)
        self.assertEqual(MaterialPrice.objects.count(), 1)
        self.assertEqual(MaterialPrice.objects.get().price_per_kg, Decimal('7.20'))

    def test_import_materials_updates_existing_price(self):
        MaterialPrice.objects.create(
            material=self.material, price_per_kg=Decimal('6.00'),
            effective_from=date(2026, 1, 1), source='purchase',
        )
        path = self._write(
            'materials.csv',
            make_csv(
                ('material_code', 'price_per_kg', 'effective_from', 'source'),
                ('A105', '7.20', '2026-01-01', 'market'),
            ),
        )
        output, error = run_command('import_prices', kind='materials', path=str(path))
        self.assertIsNone(error)
        price = MaterialPrice.objects.get()
        self.assertEqual(price.price_per_kg, Decimal('7.20'))
        self.assertEqual(price.source, 'market')

    def test_import_rates_and_customers(self):
        rates_path = self._write(
            'rates.csv',
            make_csv(
                ('code', 'label', 'unit', 'rate', 'tiers', 'effective_from'),
                ('machining', '加工费', 'per_piece', '200', '{"50": 12}', '2026-01-01'),
                ('freight', '运费', 'per_kg', '0.60', '', '2026-01-01'),
            ),
        )
        output, error = run_command('import_prices', kind='rates', path=str(rates_path))
        self.assertIsNone(error)
        self.assertEqual(ProcessRate.objects.count(), 2)
        self.assertEqual(ProcessRate.objects.get(code='machining').tiers, {'50': 12})
        self.assertEqual(ProcessRate.objects.get(code='freight').rate, Decimal('0.60'))

        customers_path = self._write(
            'customers.csv',
            make_csv(
                ('code', 'name', 'level', 'discount_pct', 'currency', 'active'),
                ('demo-a', '示例客户 A', 'A', '8', 'CNY', 'true'),
                ('demo-b', '示例客户 B', 'B', '4', 'USD', 'false'),
            ),
        )
        output, error = run_command('import_prices', kind='customers', path=str(customers_path))
        self.assertIsNone(error)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertFalse(Customer.objects.get(code='demo-b').active)

    def test_error_rows_reported_and_valid_rows_imported(self):
        path = self._write(
            'materials.csv',
            make_csv(
                ('material_code', 'price_per_kg', 'effective_from'),
                ('A105', '7.20', '2026-01-01'),   # 合法
                ('NOPE', '7.20', '2026-01-01'),   # 错误
            ),
        )
        output, error = run_command('import_prices', kind='materials', path=str(path))
        self.assertIsNotNone(error)
        self.assertIn('1 行错误', str(error))
        self.assertIn('第 3 行', output)
        self.assertEqual(MaterialPrice.objects.count(), 1)

    def test_strict_rolls_back_everything(self):
        path = self._write(
            'materials.csv',
            make_csv(
                ('material_code', 'price_per_kg', 'effective_from'),
                ('A105', '7.20', '2026-01-01'),
                ('NOPE', '7.20', '2026-01-01'),
            ),
        )
        output, error = run_command(
            'import_prices', kind='materials', path=str(path), strict=True
        )
        self.assertIsNotNone(error)
        self.assertIn('整批回滚', output)
        self.assertEqual(MaterialPrice.objects.count(), 0)

    def test_dry_run_does_not_write(self):
        path = self._write(
            'materials.csv',
            make_csv(
                ('material_code', 'price_per_kg', 'effective_from'),
                ('A105', '7.20', '2026-01-01'),
            ),
        )
        output, error = run_command(
            'import_prices', kind='materials', path=str(path), dry_run=True
        )
        self.assertIsNone(error)
        self.assertIn('dry-run', output)
        self.assertEqual(MaterialPrice.objects.count(), 0)

    def test_template_output(self):
        output, error = run_command('import_prices', kind='rates', template='-')
        self.assertIsNone(error)
        self.assertIn('code,label,unit,rate,tiers,effective_from', output)

        output, error = run_command('import_prices', template='-')
        self.assertIsNotNone(error)

    def test_missing_file_raises(self):
        output, error = run_command(
            'import_prices', kind='materials', path=str(self.root / 'nope.csv')
        )
        self.assertIsNotNone(error)


# ================================================================ Admin
class AdminRegistrationTests(TestCase):
    request = RequestFactory().get('/admin/quote/')
    request.user = AnonymousUser()

    def test_models_registered(self):
        for model in (Material, MaterialPrice, ProcessRate, Customer):
            self.assertTrue(django_admin.site.is_registered(model), model.__name__)

    def test_material_price_admin_config(self):
        model_admin = django_admin.site._registry[MaterialPrice]
        self.assertEqual(model_admin.date_hierarchy, 'effective_from')
        self.assertIn('source', model_admin.list_filter)
        self.assertIn('currency', model_admin.list_filter)
        self.assertIn('price_per_kg', model_admin.list_display)
        self.assertIn('material__code', model_admin.search_fields)

    def test_process_rate_admin_config_and_source_field(self):
        model_admin = django_admin.site._registry[ProcessRate]
        self.assertEqual(model_admin.date_hierarchy, 'effective_from')
        self.assertIn('rate', model_admin.list_display)
        self.assertIn('unit', model_admin.list_filter)
        # T5 之后 ProcessRate 有 source 字段，应出现在列表/筛选中
        self.assertIn('source', model_admin.list_display)
        self.assertIn('source', model_admin.list_filter)
        form_class = model_admin.get_form(self.request)
        choices = [value for value, _label in form_class.base_fields['source'].choices]
        self.assertIn('demo', choices)

    def test_customer_and_material_admin_config(self):
        customer_admin = django_admin.site._registry[Customer]
        self.assertIn('level', customer_admin.list_filter)
        self.assertIn('discount_pct', customer_admin.list_display)
        self.assertEqual(customer_admin.search_fields, ('code', 'name', 'payment_terms'))

        material_admin = django_admin.site._registry[Material]
        self.assertIn('family', material_admin.list_filter)
        self.assertIn('code', material_admin.search_fields)

    def test_dotted_en_material_code_is_readonly_when_editing(self):
        material = Material.objects.create(
            code='1.4301', label='1.4301 (304)', family='不锈钢 304',
            density=Decimal('7.930'),
        )
        material_admin = django_admin.site._registry[Material]
        readonly = material_admin.get_readonly_fields(self.request, material)
        self.assertIn('code', readonly)
        # 新增时仍可填写普通牌号，不含点
        self.assertNotIn('code', material_admin.get_readonly_fields(self.request, None))

        # 编辑含点牌号：code 只读 → 只改 label 应能通过表单校验
        form_class = material_admin.get_form(self.request, material)
        self.assertNotIn('code', form_class.base_fields)
        form = form_class(
            data={
                'label': '1.4301 (304) 改',
                'family': '不锈钢 304',
                'density': '7.930',
                'active': 'on',
                'sort_order': '0',
            },
            instance=material,
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())

    def test_material_price_admin_form_accepts_demo_source(self):
        model_admin = django_admin.site._registry[MaterialPrice]
        form_class = model_admin.get_form(self.request)
        choices = [value for value, _label in form_class.base_fields['source'].choices]
        self.assertIn('demo', choices)


# ================================================================ 演示种子
class SeedDemoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        output, error = run_command('seed_demo', force=True)
        assert error is None, f'seed_demo 失败：{error}\n{output}'
        cls.seed_output = output

    def test_materials_and_prices_match_prototype(self):
        self.assertEqual(Material.objects.count(), len(EXPECTED_DEMO_PRICES))
        self.assertEqual(
            set(Material.objects.values_list('code', flat=True)), set(EXPECTED_DEMO_PRICES)
        )
        self.assertEqual(
            Material.objects.get(code='F316L').density, Decimal('7.930')
        )

        prices_by_code = {
            price.material.code: price for price in MaterialPrice.objects.select_related('material')
        }
        self.assertEqual(len(prices_by_code), len(EXPECTED_DEMO_PRICES))
        for code, expected in EXPECTED_DEMO_PRICES.items():
            price = prices_by_code[code]
            self.assertEqual(price.price_per_kg, Decimal(expected), code)
            self.assertEqual(price.currency, 'CNY')
            self.assertEqual(price.effective_from, seed_demo.DEMO_EFFECTIVE_FROM)
            self.assertEqual(price.source, 'demo', f'{code} 必须明确标 demo')
            self.assertIn('演示', price.note)
        # 未被当成真实价格静默入库
        self.assertFalse(MaterialPrice.objects.exclude(source='demo').exists())

    def test_process_rates_seeded_as_demo(self):
        rates = {rate.code: rate for rate in ProcessRate.objects.all()}
        self.assertEqual(set(rates), set(seed_demo.DEMO_RATE_CODES))
        for rate in rates.values():
            self.assertEqual(rate.source, 'demo', rate.code)
            self.assertEqual(rate.effective_from, seed_demo.DEMO_EFFECTIVE_FROM)
        self.assertEqual(
            prices.rate_for('machining', date(2026, 11, 6), dn=100), Decimal('18')
        )
        # 加工档位字典是“累计上限”（services 交给 costing.machining_base 使用）；
        # pricing.rate_for 只做精确键匹配，非档位点回落到基础费率。
        self.assertEqual(
            prices.rate_for('machining', date(2026, 11, 6), dn=200), Decimal('28')
        )
        self.assertEqual(
            prices.rate_for('machining', date(2026, 11, 6), dn=300), Decimal('200')
        )
        self.assertEqual(
            prices.all_rates(date(2026, 11, 6))['machining'].tiers['350'], 45
        )
        self.assertEqual(
            prices.rate_for('freight', date(2026, 11, 6)), Decimal('0.60')
        )
        self.assertEqual(
            prices.all_rates(date(2026, 11, 6))['surface'].tiers['zinc'], 3.0
        )

    def test_customers_seeded_a_b_c(self):
        customers = {customer.code: customer for customer in Customer.objects.all()}
        self.assertEqual(set(customers), set(seed_demo.DEMO_CUSTOMER_CODES))
        self.assertEqual(customers['demo-a'].level, 'A')
        self.assertEqual(customers['demo-b'].level, 'B')
        self.assertEqual(customers['demo-c'].level, 'C')
        self.assertEqual(customers['demo-a'].discount_pct, Decimal('8.00'))
        self.assertEqual(customers['demo-c'].discount_pct, Decimal('0.00'))
        for customer in customers.values():
            self.assertIn('演示', customer.name)

    def test_seed_is_idempotent(self):
        counts = (
            Material.objects.count(),
            MaterialPrice.objects.count(),
            ProcessRate.objects.count(),
            Customer.objects.count(),
        )
        output, error = run_command('seed_demo', force=True)
        self.assertIsNone(error)
        self.assertEqual(
            counts,
            (
                Material.objects.count(),
                MaterialPrice.objects.count(),
                ProcessRate.objects.count(),
                Customer.objects.count(),
            ),
        )
        self.assertEqual(MaterialPrice.objects.count(), len(EXPECTED_DEMO_PRICES))

    def test_seed_does_not_overwrite_real_price_or_rate(self):
        material = Material.objects.get(code='F304')
        real_price = MaterialPrice.objects.get(
            material=material, effective_from=seed_demo.DEMO_EFFECTIVE_FROM
        )
        real_price.source = 'purchase'
        real_price.price_per_kg = Decimal('99.00')
        real_price.note = '真实采购价'
        real_price.save()

        machining = ProcessRate.objects.get(code='machining')
        machining.source = 'manual'
        machining.label = '加工费（真实）'
        machining.rate = Decimal('999')
        machining.save()

        output, error = run_command('seed_demo', force=True)
        self.assertIsNone(error)
        real_price.refresh_from_db()
        machining.refresh_from_db()
        self.assertEqual(real_price.price_per_kg, Decimal('99.00'))
        self.assertEqual(real_price.source, 'purchase')
        self.assertEqual(machining.rate, Decimal('999'))
        self.assertEqual(machining.source, 'manual')
        self.assertIn('跳过', output)

    def test_clear_removes_only_demo_rows(self):
        output, error = run_command('seed_demo', clear=True, force=True)
        self.assertIsNone(error)
        self.assertEqual(MaterialPrice.objects.filter(source='demo').count(), 0)
        self.assertEqual(ProcessRate.objects.count(), 0)
        self.assertEqual(Customer.objects.count(), 0)
        # 材质主数据保留
        self.assertEqual(Material.objects.count(), len(EXPECTED_DEMO_PRICES))

    def test_effective_from_option(self):
        output, error = run_command(
            'seed_demo', force=True, effective_from='2025-06-01'
        )
        self.assertIsNone(error)
        self.assertTrue(
            MaterialPrice.objects.filter(
                effective_from=date(2025, 6, 1), source='demo'
            ).exists()
        )

    def test_seeded_prices_are_usable_by_services(self):
        """种子数据不是摆设：4″ Class 150 WN A105 应按契约 §3.2 算到 170.55。"""
        standard = Standard.objects.create(
            id='asme-b16-5', name='ASME B16.5', name_zh='美标管法兰',
            short='B16.5', edition='ASME B16.5-2025',
        )
        flange_type = FlangeType.objects.create(
            slug='weld-neck', name_zh='对焊法兰', name_en='Weld Neck',
        )
        rating = Rating.objects.create(
            standard=standard, code='150', label='Class 150',
        )
        DimensionRow.objects.create(
            standard=standard, flange_type=flange_type, rating=rating,
            size='4', size_label='4″', dn=100, od=228.6, th=22.4,
            rf=157.2, rf_height=1.5, bc=190.5, bolts=8, bolt_hole=19.1,
            hub=134.9, length=74.7, hub_weld=114.3, weight=6.8,
        )
        payload = {
            'std': 'asme-b16-5',
            'type': 'weld-neck',
            'size': '4',
            'rating': '150',
            'material': 'A105',
            'qty': 10,
            'on_date': '2026-11-06',
            'design_temp_c': 38,
            'design_pressure_bar': 16,
            'surface': 'none',
            'packaging': 'wood',
            'inspections': [],
            'params': {
                'yield': 0.70,
                'machining_factor': 1.0,
                'management_pct': 6,
                'profit_pct': 12,
                'tax_pct': 13,
            },
        }
        result = services.calculate(payload)
        self.assertEqual(result['item']['net_weight'], 6.8)
        self.assertEqual(result['costs']['material'], 69.94)
        self.assertEqual(result['costs']['total'], 137.51)
        self.assertEqual(result['price']['unit_gross'], 170.55)
        snapshot = result['snapshot']
        self.assertEqual(snapshot['price_versions']['material_prices']['A105']['price_per_kg'], 7.2)
        # 演示数据必须可识别，不能被快照当成真实价格
        self.assertTrue(snapshot['demo_rates'])
        self.assertEqual(snapshot['rate_source'], services.DEMO_DEFAULTS)
        self.assertEqual(snapshot['params']['material_price_source'], 'demo')
        self.assertTrue(snapshot['params']['demo_material_price'])


class SeedDemoGuardTests(TestCase):
    """生产保护：DEBUG=False 时默认拒绝写入演示数据。"""

    @override_settings(DEBUG=False)
    def test_refuses_without_force_when_debug_false(self):
        output, error = run_command('seed_demo')
        self.assertIsNotNone(error)
        self.assertIn('--force', str(error))
        self.assertEqual(Material.objects.count(), 0)
        self.assertEqual(MaterialPrice.objects.count(), 0)

    @override_settings(DEBUG=True)
    def test_allows_without_force_when_debug_true(self):
        output, error = run_command('seed_demo')
        self.assertIsNone(error)
        self.assertEqual(Material.objects.count(), len(EXPECTED_DEMO_PRICES))
        self.assertEqual(MaterialPrice.objects.count(), len(EXPECTED_DEMO_PRICES))

    @override_settings(DEBUG=True)
    def test_dry_run_writes_nothing(self):
        output, error = run_command('seed_demo', dry_run=True)
        self.assertIsNone(error)
        self.assertIn('dry-run', output)
        self.assertEqual(Material.objects.count(), 0)
        self.assertEqual(MaterialPrice.objects.count(), 0)
