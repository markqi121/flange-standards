"""quote 后端核心单元测试：weight / costing / pricing / validation / services / api。

运行：``.venv/bin/python manage.py test quote``
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from catalog.models import DimensionRow, FlangeType, Rating, Standard

from quote import api, costing, pricing, services, validation, weight
from quote.models import (
    ApprovalLog,
    Customer,
    Material,
    MaterialPrice,
    ProcessRate,
    Quotation,
)

User = get_user_model()


def make_payload(**overrides):
    """契约 §3.2 示例输入（4″ Class 150 WN A105，qty 10，默认参数）。"""
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


class QuoteFixtureMixin:
    """共享测试夹具：ASME B16.5 / EN 1092-1 的最小 catalog 数据 + 材质价格。"""

    @classmethod
    def setUpTestData(cls):
        cls.standard = Standard.objects.create(
            id='asme-b16-5', name='ASME B16.5', name_zh='美标管法兰',
            short='B16.5', edition='ASME B16.5-2025', sort_order=0,
        )
        cls.standard_en = Standard.objects.create(
            id='en-1092-1', name='EN 1092-1', name_zh='欧标法兰',
            short='EN 1092-1', edition='EN 1092-1:2018', sort_order=3,
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
        # EN DN100 PN16 滑套：无参考重量 → 几何 C
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
        cls.material_316l = Material.objects.create(
            code='F316L', label='ASTM A182 F316L', family='不锈钢 316L',
            density=Decimal('7.930'), sort_order=1,
        )
        cls.price_old = MaterialPrice.objects.create(
            material=cls.material, price_per_kg=Decimal('5.00'),
            effective_from=date(2019, 1, 1), source='purchase',
        )
        cls.price = MaterialPrice.objects.create(
            material=cls.material, price_per_kg=Decimal('7.20'),
            effective_from=date(2020, 1, 1), source='purchase',
        )
        cls.price_316l = MaterialPrice.objects.create(
            material=cls.material_316l, price_per_kg=Decimal('35.00'),
            effective_from=date(2020, 1, 1), source='purchase',
        )
        cls.customer = Customer.objects.create(
            code='C001', name='示例客户', level='A',
            discount_pct=Decimal('5.00'), currency='CNY',
        )
        cls.user = User.objects.create_user(username='sales', password='test-pass-123')
        cls.factory = RequestFactory()

    def api_request(self, method, path, payload=None, user=None, **kwargs):
        factory = RequestFactory()
        if method == 'post':
            request = factory.post(
                path, data=json.dumps(payload or {}), content_type='application/json'
            )
        else:
            request = factory.get(path, **kwargs)
        request.user = user if user is not None else self.user
        return request


# ================================================================ 重量
class WeightTests(QuoteFixtureMixin, TestCase):
    def test_standard_weight_b_level(self):
        result = weight.resolve_weight(self.row_wn, self.material)
        self.assertEqual(result.source, 'standard')
        self.assertEqual(result.confidence, 'B')
        self.assertEqual(result.rule_version, 'w0.3')
        self.assertEqual(result.net_weight, Decimal('6.800'))

    def test_so_and_bl_standard_weight(self):
        so = weight.resolve_weight(self.row_so, self.material)
        bl = weight.resolve_weight(self.row_bl, self.material)
        self.assertEqual(so.net_weight, Decimal('5.900'))
        self.assertEqual(bl.net_weight, Decimal('7.700'))

    def test_density_correction_for_stainless(self):
        result = weight.resolve_weight(self.row_wn, self.material_316l)
        # 6.8 × 7.93 / 7.85 = 6.86930…
        self.assertEqual(result.source, 'standard')
        self.assertEqual(result.net_weight, Decimal('6.869'))

    def test_cad_verified_geometry_volume_and_weight(self):
        """4″ WN 几何体积与 CAD 一致（909987.747 mm³），校准后 6.812 kg。"""
        row = SimpleNamespace(
            flange_type_id='weld-neck', dn=100, od=228.6, th=22.4,
            rf=157.2, rf_height=1.5, bolts=8, bolt_hole=19.1,
            hub=134.9, length=74.7, hub_weld=114.3, weight=None,
        )
        material = SimpleNamespace(density=Decimal('7.85'))
        result = weight.resolve_weight(row, material)
        self.assertEqual(result.source, 'geometry')
        self.assertEqual(result.confidence, 'C')
        self.assertAlmostEqual(result.volume_mm3, 909987.747, places=2)
        self.assertEqual(result.raw_weight, Decimal('7.143'))
        self.assertEqual(result.net_weight, Decimal('6.812'))

    def test_volume_parts_decomposition(self):
        parts, volume, pipe_od, pipe_id = weight.volume_parts(self.row_wn)
        self.assertAlmostEqual(volume, parts.ring + parts.hub + parts.rf - parts.bore - parts.bolts, places=6)
        self.assertGreater(parts.ring, 0)
        self.assertGreater(parts.hub, 0)
        self.assertGreater(parts.rf, 0)
        self.assertGreater(parts.bore, 0)
        self.assertGreater(parts.bolts, 0)
        self.assertEqual((pipe_od, pipe_id), (114.3, 102.3))

    def test_en_slip_on_uses_geometry_c(self):
        result = weight.resolve_weight(self.row_en_so, self.material)
        self.assertEqual(result.source, 'geometry')
        self.assertEqual(result.confidence, 'C')
        self.assertEqual(result.net_weight, Decimal('5.190'))
        self.assertGreater(result.volume_mm3, 0)

    def test_c_includes_f_switch_removes_rf_volume(self):
        row = SimpleNamespace(
            flange_type_id='weld-neck', dn=100, od=228.6, th=22.4,
            rf=157.2, rf_height=1.5, bolts=8, bolt_hole=19.1,
            hub=134.9, length=74.7, hub_weld=114.3, weight=None,
        )
        material = SimpleNamespace(density=Decimal('7.85'))
        with_f = weight.resolve_weight(row, material, c_includes_f=False)
        without_f = weight.resolve_weight(row, material, c_includes_f=True)
        self.assertGreater(with_f.net_weight, without_f.net_weight)

    def test_result_dict_contract_keys(self):
        data = weight.resolve_weight(self.row_wn, self.material).as_dict()
        for key in ('net_weight', 'source', 'confidence', 'volume_mm3', 'parts',
                    'raw_weight', 'rule_version'):
            self.assertIn(key, data)


# ================================================================ 成本
class CostingTests(QuoteFixtureMixin, TestCase):
    def test_contract_cost_fixture(self):
        """4″ Class 150 WN A105、qty 10：成本 137.51（契约 §9）。"""
        result = costing.compute_cost(
            net_weight=Decimal('6.800'), dn=100, flange_type='weld-neck',
            qty=10, material_price=Decimal('7.20'),
        )
        costs = result.rounded()
        self.assertEqual(costs['material'], 69.94)
        self.assertEqual(costs['machining'], 20.70)
        self.assertEqual(costs['surface'], 0.0)
        self.assertEqual(costs['inspection'], 0.0)
        self.assertEqual(costs['packaging'], 35.0)
        self.assertEqual(costs['freight'], 4.08)
        self.assertEqual(costs['management'], 7.78)
        self.assertEqual(costs['total'], 137.51)
        self.assertEqual(result.lead_time_days, 8)
        self.assertTrue(result.demo_rates)

    def test_lead_time_rules(self):
        self.assertEqual(costing.lead_time_days(10, 'none', []), 8)
        self.assertEqual(costing.lead_time_days(1, 'none', []), 8)
        self.assertEqual(costing.lead_time_days(60, 'none', []), 9)
        self.assertEqual(costing.lead_time_days(10, 'zinc', []), 10)
        self.assertEqual(costing.lead_time_days(10, 'none', ['ut']), 11)
        self.assertEqual(costing.lead_time_days(10, 'none', ['third']), 13)

    def test_inspection_and_third_party(self):
        base = costing.compute_cost(
            net_weight=Decimal('6.8'), dn=100, flange_type='weld-neck',
            qty=10, material_price=Decimal('7.2'),
        )
        with_pmi = costing.compute_cost(
            net_weight=Decimal('6.8'), dn=100, flange_type='weld-neck',
            qty=10, material_price=Decimal('7.2'), inspections=['pmi'],
        )
        self.assertEqual(with_pmi.inspection_fixed, Decimal('25'))
        self.assertAlmostEqual(float(with_pmi.inspection - base.inspection), 25.0, places=2)
        with_third = costing.compute_cost(
            net_weight=Decimal('6.8'), dn=100, flange_type='weld-neck',
            qty=10, material_price=Decimal('7.2'), inspections=['third'],
        )
        expected = Decimal('0.035') * (base.material + base.machining)
        self.assertAlmostEqual(float(with_third.third_party), float(expected), places=2)

    def test_machining_tiers_by_dn(self):
        self.assertEqual(costing.machining_base(50), Decimal('12'))
        self.assertEqual(costing.machining_base(51), Decimal('18'))
        self.assertEqual(costing.machining_base(100), Decimal('18'))
        self.assertEqual(costing.machining_base(101), Decimal('28'))
        self.assertEqual(costing.machining_base(1200), Decimal('200'))

    def test_surface_and_packaging_options(self):
        result = costing.compute_cost(
            net_weight=Decimal('6.8'), dn=100, flange_type='slip-on',
            qty=1, material_price=Decimal('7.2'), surface='zinc',
            packaging='fumigated',
        )
        self.assertEqual(result.surface_cost, Decimal('20.400'))
        self.assertEqual(result.packaging, Decimal('55'))
        self.assertEqual(result.type_factor_value, Decimal('1.0'))


# ================================================================ 定价
class PricingTests(QuoteFixtureMixin, TestCase):
    def test_contract_price_fixture(self):
        """单件成本 137.5062… → 含税单价 170.55、总额 1705.50（契约 §9）。"""
        price = pricing.compute_price(
            unit_cost=Decimal('137.5062285714285714285714285'),
            qty=10, profit_pct=12, discount_pct=0, tax_pct=13,
            net_weight=Decimal('6.800'),
        )
        data = price.as_dict()
        self.assertEqual(data['unit_net'], 150.93)
        self.assertEqual(data['unit_tax'], 19.62)
        self.assertEqual(data['unit_gross'], 170.55)
        self.assertEqual(data['amount_net'], 1509.30)
        self.assertEqual(data['tax_amount'], 196.20)
        self.assertEqual(data['total_gross'], 1705.50)
        self.assertEqual(data['margin_pct'], 8.9)
        self.assertEqual(data['per_kg'], 25.08)

    def test_quantity_discount_tiers(self):
        self.assertEqual(pricing.quantity_discount_pct(1), Decimal('0'))
        self.assertEqual(pricing.quantity_discount_pct(9), Decimal('0'))
        self.assertEqual(pricing.quantity_discount_pct(10), Decimal('2'))
        self.assertEqual(pricing.quantity_discount_pct(50), Decimal('4'))
        self.assertEqual(pricing.quantity_discount_pct(200), Decimal('6'))

    def test_approval_red_lines(self):
        self.assertFalse(pricing.approval_required(0, Decimal('1705.50'), Decimal('8.9')))
        self.assertTrue(pricing.approval_required(6, Decimal('1705.50'), Decimal('8.9')))
        self.assertTrue(pricing.approval_required(0, Decimal('60000'), Decimal('8.9')))
        self.assertTrue(pricing.approval_required(0, Decimal('1705.50'), Decimal('7.9')))

    def test_price_for_picks_latest_effective(self):
        self.assertEqual(pricing.price_for(self.material, date(2019, 6, 1)), self.price_old)
        self.assertEqual(pricing.price_for(self.material, date(2026, 1, 1)), self.price)
        with self.assertRaises(pricing.PriceError):
            pricing.price_for(self.material, date(2018, 12, 31))

    def test_rate_for_tiers_and_missing(self):
        ProcessRate.objects.create(
            code='machining', label='加工费', unit='per_piece',
            rate=Decimal('100'), tiers={'<=50': 20, '<=100': 30},
            effective_from=date(2020, 1, 1),
        )
        self.assertEqual(pricing.rate_for('machining', date(2026, 1, 1), dn=100), Decimal('30'))
        self.assertEqual(pricing.rate_for('machining', date(2026, 1, 1), dn=300), Decimal('100'))
        with self.assertRaises(pricing.PriceError):
            pricing.rate_for('machining', date(2019, 1, 1))

    def test_discount_and_validity_defaults(self):
        self.assertEqual(pricing.combine_discount(Decimal('5'), Decimal('3')), Decimal('8'))
        self.assertEqual(pricing.combine_discount(Decimal('45'), Decimal('20')), Decimal('50'))
        today = date(2026, 1, 1)
        self.assertEqual(pricing.default_valid_until(today), date(2026, 1, 16))


# ================================================================ 校验
class ValidationTests(QuoteFixtureMixin, TestCase):
    def test_pt_ambient_and_interpolation(self):
        ambient = validation.validate_pt('A105', '150', 38, 16)
        self.assertEqual(ambient['status'], 'pass')
        self.assertEqual(ambient['allowed_bar'], 19.6)
        self.assertEqual(ambient['material_group'], '1.1')

        exact = validation.pt_allowable_bar('1.1', '150', 100)
        self.assertEqual(exact['allowed_bar'], 17.7)

        interpolated = validation.pt_allowable_bar('1.1', '150', 75)
        self.assertEqual(interpolated['status'], 'pass')
        self.assertAlmostEqual(interpolated['allowed_bar'], 18.4, places=3)

    def test_pt_below_minimum_is_floor(self):
        result = validation.pt_allowable_bar('1.1', '150', 20)
        self.assertEqual(result['allowed_bar'], 19.6)
        self.assertEqual(result['method'], 'floor')

    def test_pt_over_temperature_fails(self):
        result = validation.validate_pt('A105', '150', 600, 16)
        self.assertEqual(result['status'], 'fail')
        self.assertTrue(result['messages'])
        self.assertIn('最高温度', result['messages'][0])

    def test_pt_over_pressure_fails(self):
        result = validation.validate_pt('A105', '150', 38, 20)
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['allowed_bar'], 19.6)

    def test_pt_groups_21_and_23(self):
        group_21 = validation.pt_allowable_bar('2.1', '300', 700)
        self.assertEqual(group_21['allowed_bar'], 8.0)
        group_23 = validation.pt_allowable_bar('2.3', '150', 454)
        self.assertEqual(group_23['allowed_bar'], 4.6)

    def test_material_group_mapping(self):
        self.assertEqual(validation.material_group('A105'), '1.1')
        self.assertEqual(validation.material_group('F304'), '2.1')
        self.assertEqual(validation.material_group('F316L'), '2.3')
        self.assertIsNone(validation.material_group('F51'))

    def test_unconnected_material_returns_na(self):
        result = validation.validate_pt('F51', '150', 38, 16)
        self.assertEqual(result['status'], 'na')
        self.assertIsNone(result['allowed_bar'])
        self.assertTrue(result['messages'])

    def test_qty_validation(self):
        self.assertEqual(validation.validate_qty(0)['status'], 'fail')
        self.assertEqual(validation.validate_qty('abc')['status'], 'fail')
        self.assertEqual(validation.validate_qty(10)['status'], 'pass')
        self.assertEqual(validation.validate_qty(20000)['status'], 'warn')

    def test_discount_validation(self):
        self.assertEqual(validation.validate_discount(6)['status'], 'warn')
        self.assertEqual(validation.validate_discount(60)['status'], 'fail')
        self.assertEqual(validation.validate_discount(-1)['status'], 'fail')
        self.assertEqual(validation.validate_discount(0)['status'], 'pass')

    def test_validity_validation(self):
        today = timezone.localdate()
        self.assertEqual(validation.validate_validity(today - timedelta(days=1))['status'], 'fail')
        self.assertEqual(validation.validate_validity(today + timedelta(days=15))['status'], 'pass')

    def test_validate_all_contract_shape(self):
        pt = validation.validate_pt('A105', '150', 38, 16)
        result = validation.validate_all(pt=pt, checks=[validation.validate_qty(10)])
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['messages'], [])
        self.assertEqual(result['pt']['allowed_bar'], 19.6)
        self.assertEqual(result['pt']['material_group'], '1.1')


# ================================================================ 服务 / 状态机
class ServicesTests(QuoteFixtureMixin, TestCase):
    def test_next_quote_number_format_and_increment(self):
        number = services.next_quote_number()
        self.assertRegex(number, r'^FQ-\d{4}-\d{4}$')
        quote = services.create_quote(make_payload(), self.user)
        following = services.next_quote_number()
        self.assertEqual(int(following[-4:]), int(quote.number[-4:]) + 1)

    def test_calculate_contract_fixture(self):
        result = services.calculate(make_payload(), user=self.user)
        item = result['item']
        self.assertEqual(item['net_weight'], 6.8)
        self.assertEqual(item['weight_source'], 'standard')
        self.assertEqual(item['weight_confidence'], 'B')
        self.assertEqual(item['weight_rule'], 'w0.3')
        self.assertEqual(item['lead_time_days'], 8)
        self.assertEqual(result['costs']['total'], 137.51)
        self.assertEqual(result['price']['unit_net'], 150.93)
        self.assertEqual(result['price']['unit_gross'], 170.55)
        self.assertEqual(result['price']['amount_net'], 1509.30)
        self.assertEqual(result['price']['total_gross'], 1705.50)
        self.assertEqual(result['price']['margin_pct'], 8.9)
        self.assertEqual(result['validation']['status'], 'pass')
        self.assertEqual(result['validation']['pt']['allowed_bar'], 19.6)
        self.assertEqual(result['validation']['pt']['material_group'], '1.1')
        self.assertFalse(result['approval_required'])
        self.assertEqual(result['snapshot']['weight_rule'], 'w0.3')

    def test_calculate_invalid_combination(self):
        with self.assertRaises(services.InvalidCombination) as ctx:
            services.calculate(make_payload(type='slip-on', rating='2500'))
        self.assertEqual(ctx.exception.code, 'invalid_combination')
        self.assertEqual(ctx.exception.field, 'size')

    def test_calculate_missing_material_price(self):
        Material.objects.create(
            code='F304', label='ASTM A182 F304', family='不锈钢 304',
            density=Decimal('7.930'),
        )
        with self.assertRaises(services.PriceUnavailable) as ctx:
            services.calculate(make_payload(material='F304'))
        self.assertEqual(ctx.exception.code, 'price_unavailable')

    def test_calculate_pt_over_pressure_marks_fail(self):
        result = services.calculate(make_payload(design_pressure_bar=20))
        self.assertEqual(result['validation']['status'], 'fail')
        self.assertEqual(result['validation']['pt']['status'], 'fail')

    def test_calculate_unknown_material(self):
        with self.assertRaises(services.InvalidPayload):
            services.calculate(make_payload(material='NOPE'))

    def test_create_quote_persists_snapshot_and_items(self):
        quote = services.create_quote(make_payload(), self.user)
        self.assertRegex(quote.number, r'^FQ-\d{4}-\d{4}$')
        self.assertEqual(quote.status, Quotation.Status.DRAFT)
        self.assertEqual(quote.version, 1)
        self.assertEqual(quote.items.count(), 1)
        self.assertEqual(quote.subtotal, Decimal('1509.30'))
        self.assertEqual(quote.tax_amount, Decimal('196.20'))
        self.assertEqual(quote.total, Decimal('1705.50'))
        self.assertIsNotNone(quote.snapshot)
        self.assertEqual(quote.snapshot.weight_rule, 'w0.3')
        self.assertEqual(quote.snapshot.params['material_price_effective_from'], '2020-01-01')

        item = quote.items.first()
        self.assertEqual(item.net_weight, Decimal('6.800'))
        self.assertEqual(item.weight_source, 'standard')
        self.assertEqual(item.weight_confidence, 'B')
        self.assertEqual(item.unit_price, Decimal('170.55'))
        self.assertEqual(item.amount, Decimal('1509.30'))
        self.assertEqual(item.lead_time_days, 8)
        self.assertEqual(item.cost_breakdown['costs']['total'], 137.51)
        self.assertEqual(item.cost_breakdown['unit_cost'], 137.51)

    def test_cost_snapshot_replay_matches_calculation(self):
        payload = make_payload()
        calc = services.calculate(payload)
        quote = services.create_quote(payload, self.user)
        item = quote.items.first()
        self.assertEqual(item.cost_breakdown['unit_cost'], calc['costs']['total'])
        snapshot = quote.snapshot.params
        for key in ('yield', 'management_pct', 'profit_pct', 'tax_pct', 'fx_rate'):
            self.assertIn(key, snapshot)
        self.assertIn('rate_versions', snapshot)

    def test_state_machine_auto_approve_and_full_chain(self):
        quote = services.create_quote(make_payload(), self.user)
        services.transition(quote, 'submit', self.user)
        self.assertEqual(quote.status, Quotation.Status.APPROVED)
        services.transition(quote, 'send', self.user)
        self.assertEqual(quote.status, Quotation.Status.SENT)
        services.transition(quote, 'accept', self.user)
        self.assertEqual(quote.status, Quotation.Status.ACCEPTED)
        services.transition(quote, 'order', self.user)
        self.assertEqual(quote.status, Quotation.Status.ORDERED)
        self.assertEqual(quote.approval_logs.count(), 4)

    def test_state_machine_requires_approval_when_discount(self):
        quote = services.create_quote(make_payload(discount_pct=10), self.user)
        self.assertTrue(services.approval_required(quote))
        self.assertLess(services.quote_margin_pct(quote), Decimal('8'))
        services.transition(quote, 'submit', self.user)
        self.assertEqual(quote.status, Quotation.Status.PENDING)
        self.assertIsNone(quote.approved_by)
        services.transition(quote, 'approve', self.user, comment='同意')
        self.assertEqual(quote.status, Quotation.Status.APPROVED)
        self.assertEqual(quote.approved_by_id, self.user.pk)
        services.transition(quote, 'send', self.user)
        services.transition(quote, 'reject', self.user)
        self.assertEqual(quote.status, Quotation.Status.REJECTED)
        self.assertEqual(quote.approval_logs.filter(action='approve').count(), 1)

    def test_invalid_transition_raises(self):
        quote = services.create_quote(make_payload(), self.user)
        with self.assertRaises(services.InvalidTransition) as ctx:
            services.transition(quote, 'order', self.user)
        self.assertEqual(ctx.exception.http_status, 409)
        with self.assertRaises(services.InvalidPayload):
            services.transition(quote, 'nonsense', self.user)

    def test_process_rate_override_applied(self):
        ProcessRate.objects.create(
            code='freight', label='运费', unit='per_kg',
            rate=Decimal('1.00'), effective_from=date(2020, 1, 1),
        )
        result = services.calculate(make_payload())
        self.assertEqual(result['costs']['freight'], 6.8)
        # 只配置了 freight，其余仍回退演示默认值 → 不能标成真实费率
        self.assertTrue(result['snapshot']['params']['demo_rates'])
        self.assertEqual(result['snapshot']['params']['rate_source'], 'demo_mixed')
        self.assertEqual(result['snapshot']['params']['default_rate_codes'],
                         sorted(['machining', 'surface', 'packaging', 'mgmt']))

    # ---- 演示价格/费率可识别性（T5） ----
    def _create_full_real_rates(self):
        ProcessRate.objects.create(
            code='machining', label='加工费', unit='per_piece', rate=Decimal('18'),
            effective_from=date(2020, 1, 1), source='purchase',
        )
        ProcessRate.objects.create(
            code='surface', label='表面处理', unit='per_kg', rate=Decimal('0'),
            tiers={'none': 0, 'rust': 1.2, 'zinc': 3.0, 'paint': 3.8},
            effective_from=date(2020, 1, 1), source='purchase',
        )
        ProcessRate.objects.create(
            code='packaging', label='包装', unit='per_piece', rate=Decimal('35'),
            tiers={'none': 0, 'carton': 8, 'wood': 35, 'fumigated': 55},
            effective_from=date(2020, 1, 1), source='purchase',
        )
        ProcessRate.objects.create(
            code='freight', label='运费', unit='per_kg', rate=Decimal('0.60'),
            effective_from=date(2020, 1, 1), source='purchase',
        )
        ProcessRate.objects.create(
            code='mgmt', label='管理费', unit='percent', rate=Decimal('6'),
            effective_from=date(2020, 1, 1), source='purchase',
        )

    def test_non_demo_rates_and_price_mark_false(self):
        self._create_full_real_rates()
        result = services.calculate(make_payload())
        self.assertFalse(result['snapshot']['params']['demo_rates'])
        self.assertEqual(result['snapshot']['params']['rate_source'], 'process_rate')
        self.assertFalse(result['snapshot']['demo_rates'])
        self.assertFalse(result['snapshot']['params']['demo_material_price'])
        self.assertEqual(result['snapshot']['params']['material_price_source'], 'purchase')

    def test_demo_material_price_marks_true(self):
        self.price.source = 'demo'
        self.price.save(update_fields=['source'])
        result = services.calculate(make_payload())
        self.assertTrue(result['snapshot']['params']['demo_rates'])
        self.assertTrue(result['snapshot']['demo_rates'])
        self.assertTrue(result['snapshot']['params']['demo_material_price'])
        self.assertIn(result['snapshot']['params']['rate_source'],
                      ('demo_defaults', 'demo_mixed'))

    def test_demo_material_price_note_keyword_marks_true(self):
        self.price.note = '演示价格，禁止生产使用'
        self.price.save(update_fields=['note'])
        result = services.calculate(make_payload())
        self.assertTrue(result['snapshot']['params']['demo_rates'])
        self.assertTrue(result['snapshot']['params']['demo_material_price'])

    def test_demo_process_rate_source_marks_true(self):
        self._create_full_real_rates()
        ProcessRate.objects.filter(code='freight').update(source='demo')
        result = services.calculate(make_payload())
        self.assertTrue(result['snapshot']['params']['demo_rates'])
        self.assertEqual(result['snapshot']['params']['rate_source'], 'demo_mixed')
        self.assertIn('freight', result['snapshot']['params']['demo_rate_codes'])
        self.assertIn('machining', result['snapshot']['params']['real_rate_codes'])

    def test_demo_process_rate_label_keyword_marks_true(self):
        self._create_full_real_rates()
        ProcessRate.objects.filter(code='freight').update(label='运费（演示）')
        result = services.calculate(make_payload())
        self.assertTrue(result['snapshot']['params']['demo_rates'])
        self.assertIn('freight', result['snapshot']['params']['demo_rate_codes'])
        self.assertEqual(result['snapshot']['params']['rate_source'], 'demo_mixed')

    def test_all_defaults_mark_demo_defaults(self):
        result = services.calculate(make_payload())
        self.assertTrue(result['snapshot']['params']['demo_rates'])
        self.assertEqual(result['snapshot']['params']['rate_source'], 'demo_defaults')

    def test_create_quote_persists_demo_flags(self):
        self.price.source = 'demo'
        self.price.save(update_fields=['source'])
        quote = services.create_quote(make_payload(), self.user)
        self.assertTrue(quote.snapshot.params['demo_rates'])
        self.assertIn(quote.snapshot.params['rate_source'], ('demo_defaults', 'demo_mixed'))
        item = quote.items.first()
        self.assertTrue(item.cost_breakdown['params']['demo_rates'])
        self.assertEqual(item.cost_breakdown['params']['rate_source'],
                         quote.snapshot.params['rate_source'])

    def test_create_quote_real_rates_snapshot_not_demo(self):
        self._create_full_real_rates()
        quote = services.create_quote(make_payload(), self.user)
        self.assertFalse(quote.snapshot.params['demo_rates'])
        self.assertEqual(quote.snapshot.params['rate_source'], 'process_rate')

    def test_create_version_archives_old(self):
        quote = services.create_quote(make_payload(), self.user)
        services.transition(quote, 'submit', self.user)
        services.transition(quote, 'send', self.user)
        new_quote = services.create_version(quote, None, self.user)
        self.assertEqual(new_quote.version, 2)
        self.assertNotEqual(new_quote.number, quote.number)
        self.assertEqual(new_quote.status, Quotation.Status.DRAFT)
        self.assertEqual(new_quote.items.count(), 1)
        self.assertEqual(quote.status, Quotation.Status.CANCELLED)
        self.assertTrue(ApprovalLog.objects.filter(quotation=quote, action='cancel').exists())

    def test_quote_to_dict_contract_fields(self):
        quote = services.create_quote(make_payload(), self.user)
        data = services.quote_to_dict(quote)
        for key in ('number', 'status', 'version', 'subtotal', 'tax_amount', 'total',
                    'items', 'approval_required', 'margin_pct'):
            self.assertIn(key, data)
        self.assertEqual(data['items'][0]['net_weight'], 6.8)

    def test_catalog_options_cascade(self):
        options = services.catalog_options()
        self.assertIn('asme-b16-5', [std['id'] for std in options['standards']])
        self.assertIn('weld-neck', options['catalog']['asme-b16-5'])
        sizes = options['catalog']['asme-b16-5']['weld-neck']['150']
        self.assertEqual(sizes[0]['size'], '4')
        self.assertTrue(options['materials'])
        self.assertTrue(options['surfaces'])


# ================================================================ API
class ApiTests(QuoteFixtureMixin, TestCase):
    def test_api_calc_ok(self):
        request = self.api_request('post', '/quote/calc/', make_payload())
        response = api.api_calc(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertEqual(data['price']['unit_gross'], 170.55)

    def test_api_calc_invalid_combination_400(self):
        request = self.api_request('post', '/quote/calc/', make_payload(type='slip-on', rating='2500'))
        response = api.api_calc(request)
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertFalse(data['ok'])
        self.assertEqual(data['error'], 'invalid_combination')
        self.assertEqual(data['field'], 'size')

    def test_api_requires_login(self):
        from django.contrib.auth.models import AnonymousUser

        request = self.api_request('post', '/quote/calc/', make_payload(), user=AnonymousUser())
        response = api.api_calc(request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)['error'], 'auth_required')

    def test_api_create_quote(self):
        request = self.api_request('post', '/quote/', make_payload())
        response = api.api_create(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertRegex(data['number'], r'^FQ-\d{4}-\d{4}$')
        self.assertEqual(data['quote']['total'], 1705.50)
        self.assertEqual(data['quote']['items'][0]['unit_price'], 170.55)

    def test_api_status_transition(self):
        quote = services.create_quote(make_payload(), self.user)
        request = self.api_request('post', f'/quote/{quote.number}/status/', {'action': 'submit'})
        response = api.api_status(request, quote.number)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['status'], 'approved')
        self.assertEqual(data['quote']['number'], quote.number)

    def test_api_status_unknown_quote_404(self):
        request = self.api_request('post', '/quote/FQ-0000-0000/status/', {'action': 'submit'})
        response = api.api_status(request, 'FQ-0000-0000')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)['error'], 'not_found')

    def test_api_options(self):
        request = self.api_request('get', '/quote/options/')
        response = api.api_options(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertIn('standards', data)
        self.assertIn('catalog', data)
        self.assertEqual(data['weight_rule'], 'w0.3')
