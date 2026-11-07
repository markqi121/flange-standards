"""P2 后端收尾测试（T9）：on_date 取价 / 客户默认折扣 / check_prices 退出码。

运行：``.venv/bin/python manage.py test quote.tests_p2``
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from quote import services
from quote.models import Material, ProcessRate
from quote.services import RATE_CODES

from .tests_core import QuoteFixtureMixin, make_payload

REAL_RATES = {
    'machining': dict(rate=Decimal('18'), unit='per_piece', tiers={}),
    'surface': dict(
        rate=Decimal('0'), unit='per_kg',
        tiers={'none': 0, 'rust': 1.2, 'zinc': 3.0, 'paint': 3.8},
    ),
    'packaging': dict(
        rate=Decimal('35'), unit='per_piece',
        tiers={'none': 0, 'carton': 8, 'wood': 35, 'fumigated': 55},
    ),
    'inspection': dict(rate=Decimal('25'), unit='per_piece', tiers={}),
    'freight': dict(rate=Decimal('0.6'), unit='per_kg', tiers={}),
    'mgmt': dict(rate=Decimal('6'), unit='percent', tiers={}),
}


class OnDateTests(QuoteFixtureMixin, TestCase):
    """报价日（on_date）真正参与取价：实参 > payload > 今天。"""

    def test_calculate_explicit_on_date(self):
        result = services.calculate(make_payload(), on_date=date(2019, 6, 1))
        params = result['snapshot']['params']
        self.assertEqual(params['material_price_per_kg'], 5.0)
        self.assertEqual(params['material_price_effective_from'], '2019-01-01')
        self.assertEqual(result['costs']['material'], 48.57)

    def test_calculate_payload_on_date(self):
        result = services.calculate(make_payload(on_date='2019-06-01'))
        self.assertEqual(result['snapshot']['params']['material_price_per_kg'], 5.0)
        self.assertEqual(result['snapshot']['params']['material_price_effective_from'],
                         '2019-01-01')

    def test_explicit_on_date_beats_payload(self):
        result = services.calculate(
            make_payload(on_date='2020-06-01'), on_date=date(2019, 6, 1)
        )
        self.assertEqual(result['snapshot']['params']['material_price_per_kg'], 5.0)

    def test_default_uses_today_effective_price(self):
        result = services.calculate(make_payload())
        self.assertEqual(result['snapshot']['params']['material_price_per_kg'], 7.2)
        self.assertEqual(result['costs']['material'], 69.94)

    def test_create_quote_uses_payload_on_date(self):
        quote = services.create_quote(make_payload(on_date='2019-06-01'), self.user)
        item = quote.items.first()
        self.assertEqual(item.material_price, Decimal('5.00'))
        self.assertEqual(quote.snapshot.params['material_price_effective_from'], '2019-01-01')
        self.assertEqual(
            quote.snapshot.price_versions['material_prices']['A105']['price_per_kg'], 5.0
        )

    def test_create_quote_accepts_on_date_kwarg(self):
        quote = services.create_quote(make_payload(), self.user, on_date=date(2019, 6, 1))
        self.assertEqual(quote.items.first().material_price, Decimal('5.00'))
        self.assertEqual(quote.valid_until, date(2019, 6, 16))


class CustomerDiscountTests(QuoteFixtureMixin, TestCase):
    """客户默认折扣：payload 未显式传 discount_pct 时生效，显式值优先，上限 50%。"""

    def _gross_for_explicit_discount(self, discount):
        return services.calculate(make_payload(discount_pct=discount))['price']['unit_gross']

    @staticmethod
    def _payload_without_discount(**overrides):
        """模拟 payload 未传 discount_pct（None/空/缺省都算未传）。"""
        payload = make_payload(**overrides)
        payload.pop('discount_pct', None)
        return payload

    def test_customer_default_discount_applied(self):
        quote = services.create_quote(self._payload_without_discount(customer='C001'), self.user)
        item = quote.items.first()
        self.assertEqual(quote.customer_id, self.customer.pk)
        self.assertEqual(quote.snapshot.params['discount_pct'], 5.0)
        self.assertEqual(item.cost_breakdown['price']['discount_pct'], 5.0)
        self.assertEqual(item.unit_price, Decimal(str(self._gross_for_explicit_discount(5))))
        self.assertLess(item.unit_price, Decimal('170.55'))

    def test_explicit_zero_discount_wins(self):
        quote = services.create_quote(
            make_payload(customer='C001', discount_pct=0), self.user
        )
        self.assertEqual(quote.snapshot.params['discount_pct'], 0.0)
        self.assertEqual(quote.items.first().unit_price, Decimal('170.55'))

    def test_explicit_discount_wins_over_customer_default(self):
        quote = services.create_quote(
            make_payload(customer='C001', discount_pct=10), self.user
        )
        self.assertEqual(quote.snapshot.params['discount_pct'], 10.0)
        self.assertEqual(
            quote.items.first().unit_price,
            Decimal(str(self._gross_for_explicit_discount(10))),
        )

    def test_customer_discount_capped_at_50(self):
        self.customer.discount_pct = Decimal('60.00')
        self.customer.save(update_fields=['discount_pct'])
        quote = services.create_quote(self._payload_without_discount(customer='C001'), self.user)
        self.assertEqual(quote.snapshot.params['discount_pct'], 50.0)
        self.assertEqual(
            quote.items.first().unit_price,
            Decimal(str(self._gross_for_explicit_discount(50))),
        )

    def test_customer_discount_enters_cost_breakdown(self):
        quote = services.create_quote(self._payload_without_discount(customer='C001'), self.user)
        price = quote.items.first().cost_breakdown['price']
        self.assertEqual(price['discount_pct'], 5.0)
        self.assertLess(price['unit_net'], price['unit_gross'])

    def test_unknown_customer_rejected(self):
        with self.assertRaises(services.InvalidPayload):
            services.create_quote(make_payload(customer='NO-SUCH'), self.user)


class CheckPricesCommandTests(QuoteFixtureMixin, TestCase):
    """check_prices：全部真实 exit 0；任一 demo/missing/invalid exit 2。"""

    def _create_real_rates(self):
        for code in RATE_CODES:
            spec = dict(REAL_RATES[code])
            ProcessRate.objects.create(
                code=code,
                label=code,
                effective_from=date(2020, 1, 1),
                source='purchase',
                **spec,
            )

    def _run_json(self):
        out = StringIO()
        error = None
        try:
            call_command('check_prices', '--json', stdout=out)
        except CommandError as exc:
            error = exc
        return out.getvalue(), error

    def test_all_real_exits_zero(self):
        self._create_real_rates()
        output, error = self._run_json()
        self.assertIsNone(error)
        data = json.loads(output)
        self.assertTrue(data['ok'])
        self.assertEqual(data['summary']['failures'], 0)
        self.assertEqual(data['summary']['materials']['ok'], 2)
        self.assertEqual(data['summary']['process_rates']['ok'], 6)

    def test_demo_material_price_exits_two(self):
        self._create_real_rates()
        self.price.source = 'demo'
        self.price.save(update_fields=['source'])
        output, error = self._run_json()
        self.assertIsNotNone(error)
        self.assertEqual(error.returncode, 2)
        data = json.loads(output)
        self.assertFalse(data['ok'])
        failures = [f for f in data['failures'] if f['kind'] == 'material_price']
        self.assertEqual(failures[0]['material'], 'A105')
        self.assertEqual(failures[0]['status'], 'demo')

    def test_missing_process_rate_exits_two(self):
        for code in RATE_CODES:
            if code == 'freight':
                continue
            ProcessRate.objects.create(
                code=code, label=code, effective_from=date(2020, 1, 1),
                source='purchase', **REAL_RATES[code],
            )
        output, error = self._run_json()
        self.assertIsNotNone(error)
        self.assertEqual(error.returncode, 2)
        data = json.loads(output)
        failures = [f for f in data['failures'] if f['kind'] == 'process_rate']
        self.assertEqual([f['code'] for f in failures], ['freight'])
        self.assertEqual(failures[0]['status'], 'missing')

    def test_missing_material_price_exits_two(self):
        self._create_real_rates()
        Material.objects.create(
            code='F304', label='ASTM A182 F304', family='不锈钢 304',
            density=Decimal('7.930'), active=True,
        )
        output, error = self._run_json()
        self.assertIsNotNone(error)
        self.assertEqual(error.returncode, 2)
        data = json.loads(output)
        failures = [f for f in data['failures'] if f['kind'] == 'material_price']
        self.assertEqual([f['material'] for f in failures], ['F304'])
        self.assertEqual(failures[0]['status'], 'missing')

    def test_demo_process_rate_label_exits_two(self):
        self._create_real_rates()
        ProcessRate.objects.filter(code='freight').update(label='运费（演示）')
        output, error = self._run_json()
        self.assertIsNotNone(error)
        self.assertEqual(error.returncode, 2)
        data = json.loads(output)
        failures = [f for f in data['failures'] if f['kind'] == 'process_rate']
        self.assertEqual(failures[0]['code'], 'freight')
        self.assertEqual(failures[0]['status'], 'demo')

    def test_human_output_on_failure(self):
        self._create_real_rates()
        self.price.source = 'demo'
        self.price.save(update_fields=['source'])
        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command('check_prices', stdout=out)
        self.assertEqual(ctx.exception.returncode, 2)
        text = out.getvalue()
        self.assertIn('不通过', text)
        self.assertIn('演示', text)

    def test_all_real_human_output_exits_zero(self):
        self._create_real_rates()
        out = StringIO()
        call_command('check_prices', stdout=out)
        self.assertIn('通过', out.getvalue())
