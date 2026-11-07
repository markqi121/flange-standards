"""报价 Web UI 测试（T2）。

覆盖：
- 工作台 / 列表 / 详情 / A4 打印 / 价格库 页面 200 与关键文案；
- 未登录访问 /quote/* 的跳转；
- 工作台注入的级联数据与契约夹具；
- T1 JSON API 集成：/quote/options/、/quote/calc/（成功 + 契约 §8 错误）、/quote/create/ → 详情/列表；
- PDF 降级（Playwright 缺失）与成功路径；
- 金额大写等展示工具；
- 移动端 / 打印 CSS 与 JS 接入点存在。

运行：``.venv/bin/python manage.py test quote``
"""

import inspect
import json
import re
import tempfile
import unittest
from io import BytesIO
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from catalog.models import DimensionRow, FlangeType, Rating, Standard

from quote.models import Customer, Material, MaterialPrice, ProcessRate

from quote import pdf as quote_pdf_module
from quote.pdf import print_page_token, verify_print_page_token, render_quote_pdf
from quote import services
from quote.views import rmb_upper

User = get_user_model()


def make_payload(**overrides):
    """契约 §3.2 示例输入。"""
    payload = {
        'std': 'asme-b16-5',
        'type': 'weld-neck',
        'size': '4',
        'rating': '150',
        'material': 'A105',
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


class UIFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.standard = Standard.objects.create(
            id='asme-b16-5', name='ASME B16.5', name_zh='美标管法兰',
            short='B16.5', edition='ASME B16.5-2025', sort_order=0,
        )
        cls.standard_en = Standard.objects.create(
            id='en-1092-1', name='EN 1092-1', name_zh='欧标法兰',
            short='EN 1092-1', edition='EN 1092-1:2018', sort_order=4,
        )
        cls.type_wn = FlangeType.objects.create(
            slug='weld-neck', name_zh='对焊法兰', name_en='Weld Neck', sort_order=0,
        )
        cls.type_so = FlangeType.objects.create(
            slug='slip-on', name_zh='滑套法兰', name_en='Slip-On', sort_order=1,
        )
        cls.type_bl = FlangeType.objects.create(
            slug='blind', name_zh='盲板法兰', name_en='Blind', sort_order=2,
        )
        cls.rating_150 = Rating.objects.create(
            standard=cls.standard, code='150', label='Class 150', sort_order=0,
        )
        cls.rating_2500 = Rating.objects.create(
            standard=cls.standard, code='2500', label='Class 2500', sort_order=6,
        )
        cls.rating_pn16 = Rating.objects.create(
            standard=cls.standard_en, code='pn16', label='PN16', sort_order=2,
        )
        common = dict(
            standard=cls.standard, size='4', size_label='4″', dn=100,
            od=228.6, th=22.4, rf=157.2, rf_height=1.5, bc=190.5,
            bolts=8, bolt_hole=19.1,
        )
        cls.row_wn = DimensionRow.objects.create(
            flange_type=cls.type_wn, rating=cls.rating_150,
            hub=134.9, length=74.7, hub_weld=114.3, weight=6.8, **common,
        )
        cls.row_so = DimensionRow.objects.create(
            flange_type=cls.type_so, rating=cls.rating_150,
            hub=134.9, length=31.8, weight=5.9, **common,
        )
        bl_common = dict(common)
        bl_common.update(th=23.9, weight=7.7)
        cls.row_bl = DimensionRow.objects.create(
            flange_type=cls.type_bl, rating=cls.rating_150, **bl_common,
        )
        cls.row_en_so = DimensionRow.objects.create(
            standard=cls.standard_en, flange_type=cls.type_so,
            rating=cls.rating_pn16, size='dn100', size_label='DN100', dn=100,
            od=220.0, th=20.0, rf=158.0, rf_height=2.0, bc=180.0,
            bolts=8, bolt_hole=18.0, hub=140.0, length=40.0, weight=None,
        )
        cls.material = Material.objects.create(
            code='A105', label='ASTM A105 / A105N', family='碳钢',
            density=Decimal('7.850'), sort_order=0,
        )
        cls.material_price = MaterialPrice.objects.create(
            material=cls.material, price_per_kg=Decimal('7.20'),
            effective_from=date(2020, 1, 1), source='purchase',
        )
        cls.customer = Customer.objects.create(
            code='C001', name='示例客户 A（演示）', level='A',
            discount_pct=Decimal('5.00'), currency='CNY',
            payment_terms='30% 预付，余款发货前',
        )
        cls.user = User.objects.create_user(username='ui-sales', password='test-pass-123')

    def login(self):
        self.client.force_login(self.user)


def bootstrap_from_response(response):
    html = response.content.decode('utf-8')
    match = re.search(
        r'<script id="quote-bootstrap" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        raise AssertionError('报价工作台没有注入 #quote-bootstrap')
    return json.loads(match.group(1))


class WorkbenchPageTests(UIFixtureMixin, TestCase):
    def setUp(self):
        self.login()

    def test_workbench_200_and_key_copy(self):
        response = self.client.get(reverse('quote:index'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        for text in (
            '报价工作台', '标准 / 系列', '法兰类型', '压力等级', '公称尺寸 / 口径',
            '材质牌号', '设计温度', '设计压力', '表面处理', '包装',
            '成本构成', 'QUOTE / 单件报价', '压力–温度校验', '加入报价单',
            '报价清单', '报价单预览（A4）', '打印 / 保存 PDF', '保存报价单',
            'A4 一页打印',
        ):
            self.assertIn(text, html, msg=f'工作台缺少文案：{text}')

    def test_workbench_bootstrap_contains_catalog_and_contract_fixture(self):
        response = self.client.get(reverse('quote:index'))
        data = bootstrap_from_response(response)
        self.assertEqual(len(data['rows']), 4)
        self.assertEqual(data['defaults']['std'], 'asme-b16-5')
        self.assertEqual(data['defaults']['type'], 'weld-neck')
        self.assertEqual(data['defaults']['size'], '4')
        self.assertEqual(data['defaults']['material'], 'A105')
        self.assertEqual(data['endpoints']['calc'], reverse('quote:api_calc'))
        self.assertEqual(data['endpoints']['create'], reverse('quote:api_create'))
        self.assertTrue(any(item['source'] == 'contract' for item in data['fixtures']))
        fixture = next(item for item in data['fixtures'] if item['source'] == 'contract')
        self.assertFalse(fixture['response']['approval_required'])
        self.assertEqual(fixture['response']['price']['unit_gross'], 170.55)
        self.assertEqual(fixture['response']['costs']['total'], 137.51)

    def test_workbench_renders_sample_sheet_and_rmb_upper(self):
        response = self.client.get(reverse('quote:index'))
        html = response.content.decode('utf-8')
        self.assertIn('1705.50', html)
        self.assertIn('壹仟柒佰零伍元伍角', html)
        self.assertIn('预览样张', html)

    def test_workbench_customer_select_comes_from_db(self):
        response = self.client.get(reverse('quote:index'))
        data = bootstrap_from_response(response)
        self.assertTrue(any(item['code'] == 'C001' for item in data['customers']))
        customer = next(item for item in data['customers'] if item['code'] == 'C001')
        self.assertEqual(customer['discount_pct'], 5.0)
        html = response.content.decode('utf-8')
        self.assertIn('id="qCustomer"', html)
        self.assertIn('data-discount="5.00"', html)
        self.assertIn('示例客户 A（演示）', html)

    def test_css_js_in_extra_head_and_extra_scripts(self):
        response = self.client.get(reverse('quote:index'))
        html = response.content.decode('utf-8')
        head = html[:html.index('</head>')]
        self.assertIn('css/quote.css', head)
        self.assertIn('name="csrf-token"', head)
        self.assertLess(html.index('css/quote.css'), html.index('</head>'))
        self.assertGreater(html.index('js/quote.js'), html.index('</main>'))
        self.assertNotIn('css/quote.css', html[html.index('<body'):html.index('</head>')] if html.index('<body') < html.index('</head>') else '')

    def test_login_page_200(self):
        self.client.logout()
        response = self.client.get('/accounts/login/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('登录法兰报价', response.content.decode('utf-8'))

    def test_workbench_anonymous_redirects_to_login(self):
        self.client.logout()
        response = self.client.get(reverse('quote:index'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])


class PageTests(UIFixtureMixin, TestCase):
    def setUp(self):
        self.login()

    def test_list_200(self):
        response = self.client.get(reverse('quote:list'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('报价单列表', response.content.decode('utf-8'))

    def test_list_demo_samples(self):
        response = self.client.get(reverse('quote:list'), {'demo': '1'})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('FQ-2611-0001', html)
        self.assertIn('示例数据', html)

    def test_detail_demo_quote_200(self):
        response = self.client.get(reverse('quote:detail', args=['FQ-2611-0001']), {'demo': '1'})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('FQ-2611-0001', html)
        self.assertIn('报价单详情', html)
        self.assertIn('1705.50', html)
        self.assertIn('明细', html)

    def test_detail_unknown_404(self):
        response = self.client.get(reverse('quote:detail', args=['FQ-0000-0000']), {'demo': '1'})
        self.assertEqual(response.status_code, 404)

    def test_print_page_is_standalone_a4(self):
        response = self.client.get(reverse('quote:print', args=['FQ-2611-0001']), {'demo': '1'})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('quote-print-body', html)
        self.assertIn('报 价 单', html)
        self.assertIn('1705.50', html)
        self.assertIn('quote.css', html)
        self.assertNotIn('site-header', html)
        self.assertNotIn('site-footer', html)

    def test_print_page_allows_anonymous_with_pdf_token(self):
        token = print_page_token('FQ-2611-0001', demo=True)
        self.client.logout()
        response = self.client.get(
            reverse('quote:print', args=['FQ-2611-0001']),
            {'demo': '1', 'pdf_token': token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('报 价 单', response.content.decode('utf-8'))

    def test_print_page_rejects_invalid_pdf_token(self):
        self.client.logout()
        response = self.client.get(
            reverse('quote:print', args=['FQ-2611-0001']),
            {'demo': '1', 'pdf_token': 'not-a-real-token'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_print_page_token_is_bound_to_number_and_demo(self):
        token = print_page_token('FQ-2611-0001', demo=True)
        self.assertFalse(verify_print_page_token(token, 'FQ-2611-0002', demo=True))
        self.assertFalse(verify_print_page_token(token, 'FQ-2611-0001', demo=False))
        self.assertTrue(verify_print_page_token(token, 'FQ-2611-0001', demo=True))
        self.assertFalse(verify_print_page_token('bad', 'FQ-2611-0001', demo=True))

    def test_customers_page_200(self):
        response = self.client.get(reverse('quote:customers'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('客户', html)
        self.assertIn('默认折扣', html)

    def test_prices_page_200(self):
        response = self.client.get(reverse('quote:prices'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('价格库', html)
        self.assertIn('A105', html)
        self.assertIn('7.20', html)
        self.assertIn('材料利用率', html)

    def test_list_and_detail_anonymous_redirect(self):
        self.client.logout()
        for url in (reverse('quote:list'), reverse('quote:detail', args=['FQ-2611-0001'])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/accounts/login/', response['Location'])

    def test_customers_anonymous_redirect(self):
        self.client.logout()
        response = self.client.get(reverse('quote:customers'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_prices_anonymous_redirect(self):
        self.client.logout()
        response = self.client.get(reverse('quote:prices'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_prices_reads_db_and_warns_on_demo_source(self):
        response = self.client.get(reverse('quote:prices'))
        html = response.content.decode('utf-8')
        self.assertIn('材料价格', html)
        self.assertIn('A105', html)
        self.assertIn('7.20', html)
        self.assertIn('采购', html)
        # 追加一条演示费率，页面必须出现 source=demo 警示
        ProcessRate.objects.create(
            code='machining', label='加工费', unit='per_piece',
            rate=Decimal('200.00'), effective_from=date(2020, 1, 1), source='demo',
        )
        response = self.client.get(reverse('quote:prices'))
        html = response.content.decode('utf-8')
        self.assertIn('演示数据警示', html)
        self.assertIn('演示', html)
        self.assertIn('禁止', html)

    def test_prices_page_has_customer_section(self):
        response = self.client.get(reverse('quote:prices'))
        html = response.content.decode('utf-8')
        self.assertIn('客户与默认折扣', html)
        self.assertIn('C001', html)
        self.assertIn('5.0%', html)

    def test_detail_has_status_action_buttons_for_real_quote(self):
        quote = services.create_quote(
            {'incoterm': 'FOB', 'items': [make_payload()]}, self.user
        )
        detail = self.client.get(reverse('quote:detail', args=[quote.number]))
        self.assertEqual(detail.status_code, 200)
        html = detail.content.decode('utf-8')
        self.assertIn('data-status-action="submit"', html)
        self.assertIn('data-status-action="cancel"', html)
        self.assertIn(reverse('quote:api_status', args=[quote.number]), html)
        listing = self.client.get(reverse('quote:list'))
        self.assertIn('data-status-action="submit"', listing.content.decode('utf-8'))

        # 流转后按钮按新状态变化
        transition = self.client.post(
            reverse('quote:api_status', args=[quote.number]),
            data=json.dumps({'action': 'submit'}),
            content_type='application/json',
        )
        self.assertEqual(transition.status_code, 200)
        detail = self.client.get(reverse('quote:detail', args=[quote.number]))
        html = detail.content.decode('utf-8')
        self.assertIn('data-status-action="send"', html)
        self.assertIn('data-status-action="expire"', html)
        self.assertIn('data-status-action="cancel"', html)

    def test_detail_demo_quote_has_no_status_actions(self):
        response = self.client.get(reverse('quote:detail', args=['FQ-2611-0001']), {'demo': '1'})
        html = response.content.decode('utf-8')
        self.assertIn('样张不落库', html)
        self.assertNotIn('data-status-action=', html)

    def test_pdf_unavailable_degrades_to_503(self):
        with mock.patch('quote.views.render_quote_pdf', side_effect=RuntimeError('Playwright 未安装')):
            response = self.client.get(
                reverse('quote:pdf', args=['FQ-2611-0001']), {'demo': '1'}
            )
        self.assertEqual(response.status_code, 503)
        html = response.content.decode('utf-8')
        self.assertIn('PDF 生成暂不可用', html)
        self.assertIn('A4 打印页', html)
        self.assertIn('Playwright', html)

    def test_pdf_success_streams_pdf(self):
        tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        tmp.write(b'%PDF-1.4\n% fake pdf for tests\n')
        tmp.close()
        try:
            with mock.patch('quote.views.render_quote_pdf', return_value=Path(tmp.name)):
                response = self.client.get(
                    reverse('quote:pdf', args=['FQ-2611-0001']), {'demo': '1'}
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'application/pdf')
            self.assertIn('attachment;', response['Content-Disposition'])
            self.assertIn('FQ-2611-0001', response['Content-Disposition'])
            self.assertEqual(b''.join(response.streaming_content)[:5], b'%PDF-')
        finally:
            Path(tmp.name).unlink(missing_ok=True)


class ApiIntegrationTests(UIFixtureMixin, TestCase):
    def setUp(self):
        self.login()

    def post_json(self, url, payload):
        return self.client.post(
            url, data=json.dumps(payload), content_type='application/json'
        )

    def test_options_api(self):
        response = self.client.get(reverse('quote:api_options'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        codes = [item['code'] for item in data['materials']]
        self.assertIn('A105', codes)
        self.assertIn('asme-b16-5', [item['id'] for item in data['standards']])

    def test_calc_api_matches_contract_fixture(self):
        response = self.post_json(reverse('quote:api_calc'), make_payload())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertAlmostEqual(data['costs']['total'], 137.51, places=2)
        self.assertAlmostEqual(data['price']['unit_gross'], 170.55, places=2)
        self.assertEqual(data['validation']['pt']['status'], 'pass')
        self.assertIn('snapshot', data)
        self.assertEqual(data['item']['net_weight'], 6.8)

    def test_calc_api_error_contract_format(self):
        payload = make_payload(size='999')
        response = self.post_json(reverse('quote:api_calc'), payload)
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['ok'])
        self.assertEqual(data['error'], 'invalid_combination')
        self.assertEqual(data['field'], 'size')
        self.assertIn('message', data)

    def test_calc_api_requires_login(self):
        self.client.logout()
        response = self.post_json(reverse('quote:api_calc'), make_payload())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'auth_required')

    def test_create_quote_with_customer_code(self):
        payload = {
            'customer': 'C001',
            'incoterm': 'FOB',
            'payment_terms': '30% 预付',
            'notes': '项目：P-2026-118',
            'items': [make_payload(discount_pct=5)],
        }
        response = self.post_json(reverse('quote:api_create'), payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        detail = self.client.get(reverse('quote:detail', args=[data['number']]))
        html = detail.content.decode('utf-8')
        self.assertIn('示例客户 A（演示）', html)
        self.assertIn('C001', html)

    def test_create_quote_then_detail_list_print(self):
        payload = {
            'incoterm': 'FOB',
            'payment_terms': '30% 预付，余款发货前',
            'notes': '客户：ACME Piping；项目 / 询价号：P-2026-118',
            'items': [make_payload()],
        }
        response = self.post_json(reverse('quote:api_create'), payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        number = data['number']
        self.assertRegex(number, r'^FQ-\d{4}-\d{4}$')
        self.assertEqual(data['status'], 'draft')

        detail = self.client.get(reverse('quote:detail', args=[number]))
        self.assertEqual(detail.status_code, 200)
        html = detail.content.decode('utf-8')
        self.assertIn(number, html)
        self.assertIn('ACME Piping', html)
        self.assertIn('1705.50', html)

        listing = self.client.get(reverse('quote:list'))
        self.assertEqual(listing.status_code, 200)
        self.assertIn(number, listing.content.decode('utf-8'))

        printed = self.client.get(reverse('quote:print', args=[number]))
        self.assertEqual(printed.status_code, 200)
        self.assertIn(number, printed.content.decode('utf-8'))
        self.assertNotIn('site-header', printed.content.decode('utf-8'))


class DisplayHelperTests(TestCase):
    def test_rmb_upper(self):
        cases = {
            Decimal('0'): '零元整',
            Decimal('1705.50'): '壹仟柒佰零伍元伍角',
            Decimal('10'): '壹拾元整',
            Decimal('10000'): '壹万元整',
            Decimal('1005.06'): '壹仟零伍元零陆分',
        }
        for amount, expected in cases.items():
            self.assertEqual(rmb_upper(amount), expected, msg=str(amount))

    def test_static_assets_have_mobile_and_print_rules(self):
        base = Path(settings.BASE_DIR)
        css = (base / 'static/css/quote.css').read_text(encoding='utf-8')
        js = (base / 'static/js/quote.js').read_text(encoding='utf-8')
        self.assertIn('@page', css)
        self.assertIn('@media print', css)
        self.assertIn('max-width: 680px', css)
        self.assertIn('quote-sheet', css)
        self.assertIn("fetch(DATA.endpoints.calc", js)
        self.assertIn("fetch(DATA.endpoints.create", js)
        self.assertIn('quantity_discount_pct', js)
        self.assertIn('calcKey', js)
        self.assertIn('mock', js.lower())


class PermissionUiTests(UIFixtureMixin, TestCase):
    """T6 无权限文案与 T7 权限矩阵的 UI 集成（只读引用 permissions，不修改它）。"""

    def setUp(self):
        self.login()

    def _add_group(self, name):
        from django.contrib.auth.models import Group

        group, _ = Group.objects.get_or_create(name=name)
        self.user.groups.add(group)
        return group

    def _other_user(self, username='other-sales'):
        user = User.objects.create_user(username=username, password='test-pass-123')
        return user

    def _quote_by(self, user, **overrides):
        return services.create_quote({'items': [make_payload(**overrides)]}, user)

    def test_sales_scope_cost_hidden_and_status_actions(self):
        self._add_group('sales')
        other = self._other_user()
        other_quote = self._quote_by(other)
        own_quote = self._quote_by(self.user)

        listing = self.client.get(reverse('quote:list'))
        html = listing.content.decode('utf-8')
        self.assertEqual(listing.status_code, 200)
        self.assertIn(own_quote.number, html)
        self.assertNotIn(other_quote.number, html)
        self.assertIn('数据范围：仅本人', html)

        detail = self.client.get(reverse('quote:detail', args=[own_quote.number]))
        html = detail.content.decode('utf-8')
        self.assertEqual(detail.status_code, 200)
        self.assertIn('data-status-action="submit"', html)
        self.assertIn('成本明细已按角色隐藏', html)
        self.assertNotIn('材料价（本次 / 基准）', html)
        self.assertNotIn('单件成本合计', html)

        forbidden_detail = self.client.get(reverse('quote:detail', args=[other_quote.number]))
        self.assertEqual(forbidden_detail.status_code, 403)
        self.assertIn('不能查看该报价单', forbidden_detail.content.decode('utf-8'))

        forbidden_prices = self.client.get(reverse('quote:prices'))
        self.assertEqual(forbidden_prices.status_code, 403)
        self.assertIn('价格库包含成本数据', forbidden_prices.content.decode('utf-8'))

        workbench = self.client.get(reverse('quote:index'))
        self.assertEqual(workbench.status_code, 200)
        wb_html = workbench.content.decode('utf-8')
        self.assertNotIn('id="yieldIn"', wb_html)
        self.assertNotIn('id="machIn"', wb_html)
        self.assertNotIn('id="mgmtIn"', wb_html)
        self.assertIn('成本参数', wb_html)
        data = bootstrap_from_response(workbench)
        self.assertEqual(data['fixtures'], [], '销售角色不应拿到含成本的契约夹具')
        self.assertTrue(all(item['demo_price'] is None for item in data['materials']))
        self.assertTrue(data['can_create'])

    def test_engineering_read_only_sees_cost_and_prices(self):
        self._add_group('engineering')
        owner = self._other_user('owner-eng')
        quote = self._quote_by(owner)

        detail = self.client.get(reverse('quote:detail', args=[quote.number]))
        html = detail.content.decode('utf-8')
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn('data-status-action=', html)
        self.assertIn('材料价（本次 / 基准）', html)
        self.assertIn('技术', html)
        self.assertIn('单件成本合计（CNY）', html)
        self.assertIn('¥69.94', html)
        self.assertIn('管理费', html)
        self.assertIn('表面处理', html)
        self.assertIn('检测', html)

        prices = self.client.get(reverse('quote:prices'))
        self.assertEqual(prices.status_code, 200)

        workbench = self.client.get(reverse('quote:index'))
        html = workbench.content.decode('utf-8')
        self.assertIn('当前角色', html)
        self.assertIn('只读', html)
        data = bootstrap_from_response(workbench)
        self.assertFalse(data['can_create'])

    def test_manager_sees_approve_reject_buttons(self):
        self._add_group('sales_manager')
        owner = self._other_user('owner-manager')
        quote = self._quote_by(owner, discount_pct=5)
        services.transition(quote, 'submit', owner)
        quote.refresh_from_db()
        self.assertEqual(quote.status, 'pending')

        detail = self.client.get(reverse('quote:detail', args=[quote.number]))
        html = detail.content.decode('utf-8')
        self.assertEqual(detail.status_code, 200)
        self.assertIn('data-status-action="approve"', html)
        self.assertIn('data-status-action="reject"', html)
        self.assertIn('材料价（本次 / 基准）', html)
        self.assertIn('单件成本合计（CNY）', html)

    def test_finance_and_admin_see_cost_breakdown(self):
        self._add_group('finance')
        owner = self._other_user('owner-finance')
        quote = self._quote_by(owner)
        html = self.client.get(reverse('quote:detail', args=[quote.number])).content.decode('utf-8')
        self.assertIn('单件成本合计（CNY）', html)

        admin = User.objects.create_superuser('ui-admin', 'admin@example.com', 'test-pass-123')
        self.client.force_login(admin)
        html = self.client.get(reverse('quote:detail', args=[quote.number])).content.decode('utf-8')
        self.assertIn('单件成本合计（CNY）', html)

    def test_unassigned_user_gets_forbidden_and_no_scope(self):
        # 只要配置了任一角色组，兼容模式关闭；未分组用户按无权限处理
        from django.contrib.auth.models import Group

        Group.objects.get_or_create(name='engineering')
        owner = self._other_user('owner-unassigned')
        quote = self._quote_by(owner)

        workbench = self.client.get(reverse('quote:index'))
        self.assertEqual(workbench.status_code, 403)
        self.assertIn('未分配报价角色', workbench.content.decode('utf-8'))

        detail = self.client.get(reverse('quote:detail', args=[quote.number]))
        self.assertEqual(detail.status_code, 403)
        self.assertIn('无权限', detail.content.decode('utf-8'))

        listing = self.client.get(reverse('quote:list'))
        self.assertEqual(listing.status_code, 200)
        self.assertIn('无可见数据', listing.content.decode('utf-8'))

        prices = self.client.get(reverse('quote:prices'))
        self.assertEqual(prices.status_code, 403)


class PdfChromeFallbackTests(TestCase):
    """T8：Playwright 不可用时，PDF_CHROME_BIN 指向的 Chrome CLI 兜底。"""

    def test_pdf_chrome_fallback_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / 'quote.pdf'
            chrome_calls = {}

            def fake_chrome(chrome_bin, url, tmp_path):
                chrome_calls['bin'] = chrome_bin
                chrome_calls['url'] = url
                Path(tmp_path).write_bytes(b'%PDF-1.4\n% chrome fallback\n' + b'x' * 2000)

            with mock.patch.object(quote_pdf_module, '_target_path', return_value=target), \
                    mock.patch.object(quote_pdf_module, '_is_fresh', return_value=False), \
                    mock.patch.object(quote_pdf_module, '_render_with_playwright', side_effect=RuntimeError('no playwright')), \
                    mock.patch.object(quote_pdf_module, '_chrome_binary', return_value='/fake/chrome'), \
                    mock.patch.object(quote_pdf_module, '_render_with_chrome', side_effect=fake_chrome):
                result = render_quote_pdf('FQ-TEST-0001', demo=False, force=True)

            self.assertEqual(Path(result), target)
            self.assertTrue(target.exists())
            self.assertEqual(chrome_calls['bin'], '/fake/chrome')
            self.assertIn('/quote/FQ-TEST-0001/print/', chrome_calls['url'])
            self.assertIn('pdf_token=', chrome_calls['url'])
            self.assertGreater(target.stat().st_size, 1000)

    def test_pdf_no_chrome_keeps_503_semantics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / 'quote.pdf'
            with mock.patch.object(quote_pdf_module, '_target_path', return_value=target), \
                    mock.patch.object(quote_pdf_module, '_is_fresh', return_value=False), \
                    mock.patch.object(quote_pdf_module, '_render_with_playwright', side_effect=RuntimeError('no playwright')), \
                    mock.patch.object(quote_pdf_module, '_chrome_binary', return_value=''):
                with self.assertRaises(RuntimeError) as ctx:
                    render_quote_pdf('FQ-TEST-0002', demo=False, force=True)
            self.assertIn('PDF_CHROME_BIN', str(ctx.exception))
            self.assertFalse(target.exists())

    def test_chrome_binary_env_and_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = Path(tmpdir) / 'chrome'
            fake.write_text('#!/bin/sh\nexit 0\n')
            fake.chmod(0o755)
            with mock.patch.dict('os.environ', {'PDF_CHROME_BIN': str(fake)}):
                self.assertEqual(quote_pdf_module._chrome_binary(), str(fake))
            with mock.patch.dict('os.environ', {'PDF_CHROME_BIN': ''}):
                with override_settings(PDF_CHROME_BIN=str(fake)):
                    self.assertEqual(quote_pdf_module._chrome_binary(), str(fake))
                with override_settings(PDF_CHROME_BIN=str(Path(tmpdir) / 'missing')):
                    self.assertEqual(quote_pdf_module._chrome_binary(), '')

    def test_chrome_cli_command_writes_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = Path(tmpdir) / 'fake-chrome'
            fake.write_text(
                '#!/bin/sh\n'
                'for arg in "$@"; do\n'
                '  case "$arg" in\n'
                '    --print-to-pdf=*)\n'
                '      out="${arg#--print-to-pdf=}"\n'
                '      printf "%%PDF-1.4\\n%% fake chrome cli\\n" > "$out"\n'
                '      for i in $(seq 1 2000); do printf "x" >> "$out"; done\n'
                '      ;;\n'
                '  esac\n'
                'done\n'
                'exit 0\n'
            )
            fake.chmod(0o755)
            out = Path(tmpdir) / 'out.pdf'
            quote_pdf_module._render_with_chrome(str(fake), 'http://127.0.0.1:8000/quote/X/print/', out)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 1000)
            self.assertTrue(out.read_bytes().startswith(b'%PDF-'))


class ExcelExportTests(UIFixtureMixin, TestCase):
    """T11：报价单 Excel 导出。"""

    CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

    def setUp(self):
        self.login()

    def _quote(self, user=None, **overrides):
        return services.create_quote({'items': [make_payload(**overrides)]}, user or self.user)

    def _load(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], self.CONTENT_TYPE)
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn('.xlsx', response['Content-Disposition'])
        return load_workbook(BytesIO(response.content))

    def _texts(self, workbook):
        return [
            str(cell.value)
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if cell.value is not None
        ]

    def _numbers(self, workbook):
        values = []
        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                        values.append(float(cell.value))
        return values

    def test_detail_excel_200_workbook_and_amounts(self):
        quote = self._quote()
        response = self.client.get(reverse('quote:excel', args=[quote.number]))
        workbook = self._load(response)
        self.assertIn(quote.number, response['Content-Disposition'])
        self.assertIn(quote.number, workbook.sheetnames)

        texts = self._texts(workbook)
        numbers = self._numbers(workbook)
        self.assertIn('报 价 单', texts)
        self.assertTrue(any(quote.number in text for text in texts))
        self.assertTrue(any('B16.5' in text and '对焊' in text for text in texts))
        self.assertIn(1705.5, numbers, 'Excel 含税合计与报价单不一致')
        self.assertIn(1509.3, numbers, 'Excel 不含税金额与报价单不一致')
        self.assertIn(10.0, numbers, 'Excel 数量缺失')
        self.assertTrue(any('数据来源' in text for text in texts))
        self.assertNotIn('单件成本合计', texts, '默认（兼容模式）不应导出成本区块')

        worksheet = workbook[quote.number]
        self.assertTrue(worksheet.print_area)
        self.assertIn('A1', worksheet.print_area.replace('$', ''))
        self.assertEqual(worksheet.page_setup.orientation, 'landscape')
        self.assertEqual(str(worksheet.page_setup.paperSize), '9')

    def test_engineering_excel_includes_cost_block(self):
        self.user.groups.add(Group.objects.create(name='engineering'))
        owner = User.objects.create_user(username='excel-owner-eng', password='test-pass-123')
        quote = self._quote(owner)
        response = self.client.get(reverse('quote:excel', args=[quote.number]))
        workbook = self._load(response)
        texts = self._texts(workbook)
        self.assertIn('单件成本明细（CNY，仅授权角色可见）', texts)
        self.assertIn('单件成本合计', texts)
        self.assertIn('材料', texts)
        self.assertIn('管理费', texts)
        self.assertIn('动态价格参数（本次 / 基准，仅授权角色可见）', texts)
        self.assertIn('材料价（本次 / 基准）', texts)
        self.assertIn('工资率（本次）', texts)

    def test_sales_excel_has_no_cost_and_cannot_export_other(self):
        self.user.groups.add(Group.objects.create(name='sales'))
        own = self._quote()
        response = self.client.get(reverse('quote:excel', args=[own.number]))
        workbook = self._load(response)
        texts = self._texts(workbook)
        self.assertNotIn('单件成本合计', texts)
        self.assertNotIn('单件成本明细（CNY，仅授权角色可见）', texts)
        self.assertNotIn('动态价格参数（本次 / 基准，仅授权角色可见）', texts)

        other_user = User.objects.create_user(username='excel-other-sales', password='test-pass-123')
        other = self._quote(other_user)
        forbidden = self.client.get(reverse('quote:excel', args=[other.number]))
        self.assertEqual(forbidden.status_code, 403)

    def test_list_export_current_filter(self):
        draft = self._quote()
        approved = self._quote()
        services.transition(approved, 'submit', self.user)
        approved.refresh_from_db()
        self.assertNotEqual(draft.status, approved.status)

        response = self.client.get(reverse('quote:export') + '?status=draft')
        workbook = self._load(response)
        self.assertIn('报价单列表', workbook.sheetnames)
        self.assertIn(draft.number, workbook.sheetnames)
        self.assertNotIn(approved.number, workbook.sheetnames)
        self.assertIn(draft.number, self._texts(workbook))

    def test_excel_requires_login(self):
        quote = self._quote()
        self.client.logout()
        for url in (
            reverse('quote:excel', args=[quote.number]),
            reverse('quote:export'),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/accounts/login/', response['Location'])

    def test_excel_buttons_present(self):
        quote = self._quote()
        detail = self.client.get(reverse('quote:detail', args=[quote.number]))
        self.assertIn(reverse('quote:excel', args=[quote.number]), detail.content.decode('utf-8'))
        listing = self.client.get(reverse('quote:list'))
        self.assertIn(reverse('quote:export'), listing.content.decode('utf-8'))
        self.assertIn(reverse('quote:excel', args=[quote.number]), listing.content.decode('utf-8'))


def _dynamic_backend_ready():
    """T13 后端（params.material_price_per_kg / machining_rate）是否已实现。"""
    try:
        from quote import services as quote_services

        source = inspect.getsource(quote_services._resolve_params)
        return 'material_price_per_kg' in source and 'machining_rate' in source
    except Exception:
        return False


class DynamicPriceUiTests(UIFixtureMixin, TestCase):
    """T14：本次材料价 / 工资率覆盖 UI。"""

    def setUp(self):
        self.login()

    def _add_group(self, name):
        self.user.groups.add(Group.objects.create(name=name))

    def test_cost_role_workbench_has_override_controls(self):
        self._add_group('engineering')
        response = self.client.get(reverse('quote:index'))
        html = response.content.decode('utf-8')
        data = bootstrap_from_response(response)
        self.assertTrue(data['can_view_cost'])
        for element_id in (
            'matPriceOverride', 'matPriceResetBtn', 'machiningRateIn',
            'pcMatSource', 'pcMachRate',
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('本次覆盖材料价', html)
        self.assertIn('工资率 / 加工费（元/件）', html)
        # 默认展示价格库材料价
        self.assertIn('value="7.2"', html)

    def test_sales_workbench_hides_override_controls(self):
        self._add_group('sales')
        response = self.client.get(reverse('quote:index'))
        html = response.content.decode('utf-8')
        data = bootstrap_from_response(response)
        self.assertFalse(data['can_view_cost'])
        for element_id in ('matPriceOverride', 'matPriceResetBtn', 'machiningRateIn'):
            self.assertNotIn(f'id="{element_id}"', html)
        self.assertIn('当前角色不可查看材料价', html)

    def test_quote_js_wires_dynamic_overrides(self):
        js = (Path(settings.BASE_DIR) / 'static' / 'js' / 'quote.js').read_text('utf-8')
        for marker in (
            'params.material_price_per_kg',
            'params.machining_rate',
            'isMaterialOverride',
            'matPriceOverride',
            'matPriceResetBtn',
            'machiningRateIn',
            'material_price_source',
            'machining_rate_source',
            '本次覆盖材料价',
        ):
            self.assertIn(marker, js)
        # 未覆盖时不得携带手工价：条件分支必须同时检查 canViewCost 与覆盖开关
        self.assertIn('if (canViewCost() && isMaterialOverride())', js)

    def test_snapshot_meta_exposes_manual_sources(self):
        self._add_group('engineering')
        quote = services.create_quote({'items': [make_payload()]}, self.user)
        params = dict(quote.snapshot.params or {})
        params.update({
            'material_price_per_kg': 8.0,
            'material_price_base': 7.2,
            'material_price_source': 'manual',
            'machining_rate': 150.0,
            'machining_rate_source': 'manual',
            'rate_source': 'manual_override',
        })
        quote.snapshot.params = params
        quote.snapshot.save(update_fields=['params'])
        response = self.client.get(reverse('quote:detail', args=[quote.number]))
        self.assertEqual(response.status_code, 200)
        meta = response.context['quote']['snapshot_meta']
        self.assertEqual(meta['material_price'], 8.0)
        self.assertEqual(meta['material_price_base'], 7.2)
        self.assertEqual(meta['material_price_source'], 'manual')
        self.assertEqual(meta['machining_rate'], 150.0)
        self.assertEqual(meta['machining_rate_source'], 'manual')
        # 现有详情模板的金额快照区展示实际材料价
        self.assertIn('¥8.00', response.content.decode('utf-8'))

    @unittest.skipUnless(_dynamic_backend_ready(), 'T13 后端动态参数尚未就绪')
    def test_calc_payload_with_manual_overrides_roundtrip(self):
        payload = make_payload()
        payload['params']['material_price_per_kg'] = 8.00
        payload['params']['machining_rate'] = 150.00
        response = self.client.post(
            reverse('quote:api_calc'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        snapshot_params = data['snapshot']['params']
        self.assertEqual(snapshot_params['material_price_source'], 'manual')
        self.assertEqual(snapshot_params['machining_rate_source'], 'manual')
        self.assertAlmostEqual(snapshot_params['material_price_per_kg'], 8.0, places=2)
        self.assertAlmostEqual(snapshot_params['machining_rate'], 150.0, places=2)
        self.assertGreater(snapshot_params.get('material_price_base') or 0, 0)

    @unittest.skipUnless(_dynamic_backend_ready(), 'T13 后端动态参数尚未就绪')
    def test_calc_payload_only_machining_rate(self):
        payload = make_payload()
        payload['params']['machining_rate'] = 150.00
        response = self.client.post(
            reverse('quote:api_calc'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        params = response.json()['snapshot']['params']
        self.assertEqual(params['machining_rate_source'], 'manual')
        self.assertAlmostEqual(params['machining_rate'], 150.0, places=2)
        # 未覆盖材料价：来源不应是 manual
        self.assertNotEqual(params.get('material_price_source'), 'manual')

    @unittest.skipUnless(_dynamic_backend_ready(), 'T13 后端动态参数尚未就绪')
    def test_calc_payload_without_override_uses_library(self):
        response = self.client.post(
            reverse('quote:api_calc'),
            data=json.dumps(make_payload()),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        params = response.json()['snapshot']['params']
        self.assertNotEqual(params.get('material_price_source'), 'manual')
        self.assertNotEqual(params.get('machining_rate_source'), 'manual')

    @unittest.skipUnless(_dynamic_backend_ready(), 'T13 后端动态参数尚未就绪')
    def test_sales_manual_override_rejected(self):
        self._add_group('sales')
        payload = make_payload()
        payload['params']['material_price_per_kg'] = 8.00
        response = self.client.post(
            reverse('quote:api_calc'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    @unittest.skipUnless(_dynamic_backend_ready(), 'T13 后端动态参数尚未就绪')
    def test_dynamic_override_changes_cost_and_snapshot(self):
        base_payload = make_payload()
        manual_payload = make_payload()
        manual_payload['params']['material_price_per_kg'] = 8.00
        base = self.client.post(
            reverse('quote:api_calc'), data=json.dumps(base_payload),
            content_type='application/json').json()
        manual = self.client.post(
            reverse('quote:api_calc'), data=json.dumps(manual_payload),
            content_type='application/json').json()
        self.assertGreater(manual['costs']['material'], base['costs']['material'])
        self.assertEqual(manual['snapshot']['params']['material_price_source'], 'manual')


class DynamicPriceSavedSnapshotTests(UIFixtureMixin, TestCase):
    """T14：保存后的报价单快照保留本次材料价 / 工资率（后端 T13）。"""

    def setUp(self):
        self.login()
        # engineering 只读，不能建单；保存快照用 sales_manager（可建单且可见成本）
        self.user.groups.add(Group.objects.create(name='sales_manager'))

    @unittest.skipUnless(_dynamic_backend_ready(), 'T13 后端动态参数尚未就绪')
    def test_created_quote_keeps_manual_snapshot_and_detail_exposes_it(self):
        payload = {
            'incoterm': 'FOB',
            'items': [make_payload()],
        }
        payload['items'][0]['params'].update({
            'material_price_per_kg': 8.00,
            'machining_rate': 150.00,
        })
        response = self.client.post(
            reverse('quote:api_create'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        number = response.json()['number']
        detail = self.client.get(reverse('quote:detail', args=[number]))
        self.assertEqual(detail.status_code, 200)
        meta = detail.context['quote']['snapshot_meta']
        self.assertEqual(meta['material_price'], 8.0)
        self.assertEqual(meta['material_price_source'], 'manual')
        self.assertEqual(meta['machining_rate'], 150.0)
        self.assertEqual(meta['machining_rate_source'], 'manual')
        html = detail.content.decode('utf-8')
        self.assertIn('材料价（本次 / 基准）', html)
        self.assertIn('¥8.00', html)
        self.assertIn('¥7.20', html)
        self.assertIn('本次覆盖', html)
        self.assertIn('工资率（本次）', html)
        self.assertIn('¥150.00 元/件', html)
        self.assertIn('工资率来源', html)


class DynamicPriceDocumentDisplayTests(UIFixtureMixin, TestCase):
    """T15：动态参数在详情 / A4 / Excel 的展示与 sales 隐藏。"""

    def setUp(self):
        self.login()
        self.user.groups.add(Group.objects.create(name='sales_manager'))

    def _quote_with_overrides(self, user=None):
        payload = {'incoterm': 'FOB', 'items': [make_payload()]}
        payload['items'][0]['params'].update({
            'material_price_per_kg': 8.00,
            'machining_rate': 150.00,
        })
        return services.create_quote(payload, user or self.user)

    def test_detail_shows_dynamic_snapshot_rows(self):
        quote = self._quote_with_overrides()
        response = self.client.get(reverse('quote:detail', args=[quote.number]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        for text in (
            '材料价（本次 / 基准）', '材料价来源', '本次覆盖',
            '工资率（本次）', '工资率来源', '¥150.00 元/件',
            '¥8.00', '¥7.20', '生效',
        ):
            self.assertIn(text, html)

    def test_print_sheet_shows_dynamic_cost_params(self):
        quote = self._quote_with_overrides()
        response = self.client.get(reverse('quote:print', args=[quote.number]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('成本参数', html)
        self.assertIn('材料价 本次', html)
        self.assertIn('基准', html)
        self.assertIn('本次覆盖', html)
        self.assertIn('工资率', html)
        self.assertIn('150.00 元/件', html)
        self.assertNotIn('site-header', html)

    def test_sales_print_and_detail_do_not_show_cost_params(self):
        sales = User.objects.create_user(username='doc-sales', password='test-pass-123')
        sales.groups.add(Group.objects.get_or_create(name='sales')[0])
        quote = services.create_quote({'items': [make_payload()]}, sales)
        self.client.force_login(sales)
        detail = self.client.get(reverse('quote:detail', args=[quote.number]))
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.content.decode('utf-8')
        self.assertNotIn('材料价（本次 / 基准）', detail_html)
        self.assertNotIn('工资率（本次）', detail_html)

        printed = self.client.get(reverse('quote:print', args=[quote.number]))
        self.assertEqual(printed.status_code, 200)
        print_html = printed.content.decode('utf-8')
        self.assertNotIn('成本参数', print_html)
        self.assertNotIn('本次覆盖', print_html)

    def test_pdf_token_print_shows_manual_for_manager_hides_for_sales(self):
        # 授权角色（manager）的本次覆盖：PDF 渲染 token 访问也可见（打印/PDF 一致）
        manager_quote = self._quote_with_overrides()
        token = print_page_token(manager_quote.number)
        self.client.logout()
        response = self.client.get(
            reverse('quote:print', args=[manager_quote.number]), {'pdf_token': token})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('成本参数', html)
        self.assertIn('本次覆盖', html)
        self.assertIn('¥8.00', html)
        self.assertIn('150.00', html)

        # sales 的报价：token 打印也不得泄露成本参数
        sales = User.objects.create_user(username='doc-sales-token', password='test-pass-123')
        sales.groups.add(Group.objects.get_or_create(name='sales')[0])
        sales_quote = services.create_quote({'items': [make_payload()]}, sales)
        sales_token = print_page_token(sales_quote.number)
        response = self.client.get(
            reverse('quote:print', args=[sales_quote.number]), {'pdf_token': sales_token})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertNotIn('成本参数', html)
        self.assertNotIn('本次覆盖', html)

    @unittest.skipUnless(_dynamic_backend_ready(), 'T13 后端动态参数尚未就绪')
    def test_excel_contains_dynamic_source_and_keeps_amounts(self):
        quote = self._quote_with_overrides()
        response = self.client.get(reverse('quote:excel', args=[quote.number]))
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content))
        texts = [
            str(cell.value)
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if cell.value is not None
        ]
        self.assertIn('动态价格参数（本次 / 基准，仅授权角色可见）', texts)
        self.assertTrue(any('本次覆盖' in text for text in texts))
        self.assertTrue(any('工资率（本次）' in text for text in texts))
        self.assertTrue(any('150.00' in text for text in texts))
        self.assertTrue(any('材料价（本次 / 基准）' in text for text in texts))
        # 金额仍与报价单一致
        numbers = [
            float(cell.value)
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool)
        ]
        self.assertIn(float(quote.total), numbers)

    def test_sales_excel_has_no_dynamic_source(self):
        sales = User.objects.create_user(username='doc-sales-excel', password='test-pass-123')
        sales.groups.add(Group.objects.get_or_create(name='sales')[0])
        quote = services.create_quote({'items': [make_payload()]}, sales)
        self.client.force_login(sales)
        response = self.client.get(reverse('quote:excel', args=[quote.number]))
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content))
        texts = [
            str(cell.value)
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if cell.value is not None
        ]
        self.assertNotIn('动态价格参数（本次 / 基准，仅授权角色可见）', texts)
        self.assertFalse(any('本次覆盖' in text for text in texts))
