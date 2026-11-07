"""动态价格参数测试（T13）：本次材料价 / 工资率覆盖、快照、权限、历史快照。

运行：``.venv/bin/python manage.py test quote.tests_dynamic``
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.core.management import call_command
from django.test import RequestFactory, TestCase

from quote import api, services
from quote.models import CostSnapshot, Material, ProcessRate

from .tests_core import QuoteFixtureMixin, make_payload

User = get_user_model()


def make_dynamic(**param_overrides):
    """契约 §3.2 payload + 本次动态成本参数（params.*）。"""
    payload = make_payload()
    payload['params'].update(param_overrides)
    return payload


class DynamicFixtureMixin(QuoteFixtureMixin):
    """在 T7 角色组已初始化的前提下建 sales / engineering 用户。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command('seed_roles', verbosity=0)
        cls.sales_user = User.objects.create_user(username='dyn-sales', password='test-pass-123')
        cls.sales_user.groups.add(Group.objects.get(name='sales'))
        cls.eng_user = User.objects.create_user(username='dyn-eng', password='test-pass-123')
        cls.eng_user.groups.add(Group.objects.get(name='engineering'))


class DynamicPriceTests(DynamicFixtureMixin, TestCase):
    # ------------------------------------------------------------ 库价 fallback
    def test_library_price_fallback_when_no_manual(self):
        result = services.calculate(make_payload(), user=self.eng_user)
        params = result['snapshot']['params']
        self.assertEqual(result['costs']['material'], 69.94)
        self.assertEqual(params['material_price_per_kg'], 7.2)
        self.assertEqual(params['material_price_base'], 7.2)
        self.assertEqual(params['material_price_origin'], 'library')
        self.assertEqual(params['material_price_source'], 'purchase')
        self.assertEqual(params['material_price_library_source'], 'purchase')
        self.assertEqual(params['machining_rate'], 18.0)
        self.assertEqual(params['machining_rate_source'], 'default')
        self.assertFalse(params['demo_material_price'])

    def test_manual_material_price_changes_cost_and_price(self):
        base = services.calculate(make_payload(), user=self.eng_user)
        result = services.calculate(
            make_dynamic(material_price_per_kg=10), user=self.eng_user
        )
        params = result['snapshot']['params']
        self.assertEqual(result['costs']['material'], 97.14)
        self.assertGreater(result['price']['unit_gross'], base['price']['unit_gross'])
        self.assertEqual(params['material_price_per_kg'], 10.0)
        self.assertEqual(params['material_price_base'], 7.2)
        self.assertEqual(params['material_price_origin'], 'manual')
        self.assertEqual(params['material_price_source'], 'manual')
        self.assertEqual(params['material_price_library_source'], 'purchase')
        self.assertFalse(params['demo_material_price'])
        material_versions = result['snapshot']['price_versions']['material_prices']['A105']
        self.assertEqual(material_versions['actual_price_per_kg'], 10.0)
        self.assertEqual(material_versions['price_per_kg'], 7.2)

    def test_manual_material_price_without_library_price(self):
        Material.objects.create(
            code='F304X', label='ASTM A182 F304', family='不锈钢 304',
            density=Decimal('7.930'),
        )
        payload = make_dynamic(material_price_per_kg=28)
        payload['material'] = 'F304X'
        result = services.calculate(payload, user=self.eng_user)
        params = result['snapshot']['params']
        # F304X 密度 7.93：standard 参考重量按密度修正后 6.869 kg → 材料费 274.76
        self.assertEqual(result['costs']['material'], 274.76)
        self.assertEqual(params['material_price_per_kg'], 28.0)
        self.assertIsNone(params['material_price_base'])
        self.assertEqual(params['material_price_origin'], 'manual')
        self.assertEqual(params['material_price_source'], 'manual')

    def test_missing_library_price_still_errors_without_manual(self):
        Material.objects.create(
            code='F304Y', label='ASTM A182 F304', family='不锈钢 304',
            density=Decimal('7.930'),
        )
        payload = make_payload()
        payload['material'] = 'F304Y'
        with self.assertRaises(services.PriceUnavailable):
            services.calculate(payload, user=self.eng_user)

    # ------------------------------------------------------------ 手工工资率
    def test_manual_machining_rate_changes_machining_cost(self):
        base = services.calculate(make_payload(), user=self.eng_user)
        result = services.calculate(make_dynamic(machining_rate=50), user=self.eng_user)
        params = result['snapshot']['params']
        self.assertEqual(base['costs']['machining'], 20.70)
        self.assertEqual(result['costs']['machining'], 57.50)
        self.assertGreater(result['price']['unit_gross'], base['price']['unit_gross'])
        self.assertEqual(params['machining_rate'], 50.0)
        self.assertEqual(params['machining_rate_source'], 'manual')
        self.assertEqual(params['rate_source'], 'manual_override')
        self.assertNotIn('machining', params['real_rate_codes'])
        self.assertNotIn('machining', params['default_rate_codes'])

    def test_manual_machining_zero_allowed(self):
        result = services.calculate(make_dynamic(machining_rate=0), user=self.eng_user)
        self.assertEqual(result['costs']['machining'], 0.0)
        self.assertEqual(result['snapshot']['params']['machining_rate'], 0.0)
        self.assertEqual(result['snapshot']['params']['machining_rate_source'], 'manual')

    def test_manual_machining_overrides_process_rate(self):
        ProcessRate.objects.create(
            code='machining', label='加工费', unit='per_piece', rate=Decimal('18'),
            effective_from=date(2020, 1, 1), source='purchase',
        )
        base = services.calculate(make_payload(), user=self.eng_user)
        self.assertEqual(base['snapshot']['params']['machining_rate'], 18.0)
        self.assertEqual(base['snapshot']['params']['machining_rate_source'], 'process')
        manual = services.calculate(make_dynamic(machining_rate=50), user=self.eng_user)
        self.assertEqual(manual['costs']['machining'], 57.50)
        self.assertEqual(manual['snapshot']['params']['machining_rate_source'], 'manual')
        self.assertEqual(manual['snapshot']['params']['rate_source'], 'manual_override')

    # ------------------------------------------------------------ 持久化
    def test_create_quote_persists_manual_material_price(self):
        quote = services.create_quote(
            make_dynamic(material_price_per_kg=10), self.eng_user
        )
        item = quote.items.first()
        self.assertEqual(item.material_price, Decimal('10.00'))
        self.assertEqual(quote.snapshot.params['material_price_per_kg'], 10.0)
        self.assertEqual(quote.snapshot.params['material_price_base'], 7.2)
        self.assertEqual(quote.snapshot.params['material_price_origin'], 'manual')
        self.assertEqual(item.cost_breakdown['costs']['material'], 97.14)
        self.assertEqual(item.cost_breakdown['params']['material_price_origin'], 'manual')

    def test_create_quote_persists_manual_machining_rate(self):
        quote = services.create_quote(make_dynamic(machining_rate=50), self.eng_user)
        item = quote.items.first()
        self.assertEqual(quote.snapshot.params['machining_rate'], 50.0)
        self.assertEqual(quote.snapshot.params['machining_rate_source'], 'manual')
        self.assertEqual(quote.snapshot.params['rate_source'], 'manual_override')
        self.assertEqual(item.cost_breakdown['costs']['machining'], 57.5)
        self.assertEqual(item.cost_breakdown['params']['machining_rate'], 50.0)

    # ------------------------------------------------------------ 权限（403 语义）
    def test_sales_manual_material_price_forbidden(self):
        with self.assertRaises(services.PermissionDenied) as ctx:
            services.calculate(make_dynamic(material_price_per_kg=10), user=self.sales_user)
        self.assertEqual(ctx.exception.code, 'permission_denied')
        self.assertEqual(ctx.exception.http_status, 403)
        self.assertEqual(ctx.exception.field, 'params')

    def test_sales_manual_machining_rate_forbidden(self):
        with self.assertRaises(services.PermissionDenied):
            services.calculate(make_dynamic(machining_rate=50), user=self.sales_user)

    def test_sales_without_manual_override_is_allowed(self):
        result = services.calculate(make_payload(), user=self.sales_user)
        self.assertEqual(result['price']['unit_gross'], 170.55)

    def test_internal_none_user_can_use_manual_override(self):
        result = services.calculate(make_dynamic(material_price_per_kg=10))
        self.assertEqual(result['snapshot']['params']['material_price_origin'], 'manual')
        self.assertEqual(result['costs']['material'], 97.14)

    def test_anonymous_user_treated_as_internal(self):
        result = services.calculate(
            make_dynamic(material_price_per_kg=10), user=AnonymousUser()
        )
        self.assertEqual(result['snapshot']['params']['material_price_origin'], 'manual')

    def test_api_calc_sales_manual_override_403(self):
        factory = RequestFactory()
        request = factory.post(
            '/quote/calc/',
            data=json.dumps(make_dynamic(material_price_per_kg=10)),
            content_type='application/json',
        )
        request.user = self.sales_user
        response = api.api_calc(request)
        self.assertEqual(response.status_code, 403)
        data = json.loads(response.content)
        self.assertFalse(data['ok'])
        self.assertEqual(data['error'], 'permission_denied')

    def test_api_calc_engineering_manual_override_ok(self):
        factory = RequestFactory()
        request = factory.post(
            '/quote/calc/',
            data=json.dumps(make_dynamic(machining_rate=50)),
            content_type='application/json',
        )
        request.user = self.eng_user
        response = api.api_calc(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['snapshot']['params']['rate_source'], 'manual_override')
        self.assertEqual(data['costs']['machining'], 57.5)

    # ------------------------------------------------------------ 参数校验
    def test_invalid_dynamic_params(self):
        for payload in (
            make_dynamic(material_price_per_kg=0),
            make_dynamic(material_price_per_kg=-1),
            make_dynamic(machining_rate=-1),
        ):
            with self.assertRaises(services.InvalidPayload):
                services.calculate(payload, user=self.eng_user)

    # ------------------------------------------------------------ 历史快照不变
    def test_history_snapshot_not_rewritten(self):
        quote = services.create_quote(make_payload(), self.eng_user)
        snapshot_id = quote.snapshot_id
        old_params = dict(quote.snapshot.params)
        old_item_price = quote.items.first().material_price
        old_snapshot_price = quote.items.first().cost_breakdown['unit_cost']

        self.price.price_per_kg = Decimal('9.99')
        self.price.save(update_fields=['price_per_kg'])
        services.create_quote(make_dynamic(material_price_per_kg=11.5), self.eng_user)

        snapshot = CostSnapshot.objects.get(pk=snapshot_id)
        self.assertEqual(snapshot.params, old_params)
        quote.refresh_from_db()
        self.assertEqual(quote.items.first().material_price, old_item_price)
        self.assertEqual(
            quote.items.first().cost_breakdown['unit_cost'], old_snapshot_price
        )
        self.assertEqual(quote.snapshot.params['material_price_per_kg'], 7.2)
