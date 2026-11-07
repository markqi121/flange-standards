"""权限矩阵测试（T7）：5 角色 × 成本/价格/审批/本人/全部 + API 不泄露成本。

运行：``.venv/bin/python manage.py test quote.tests_permissions``
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.core.management import call_command
from django.test import RequestFactory, TestCase

from catalog.models import DimensionRow, FlangeType, Rating, Standard

from quote import api, permissions, services
from quote.models import Material, MaterialPrice, Quotation

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


class PermissionFixtureMixin:
    """建 5 个角色用户 + 最小 catalog/价格数据 + 3 张报价单。"""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_roles', verbosity=0)

        cls.users = {}
        for role in permissions.ROLE_NAMES:
            user = User.objects.create_user(username=f'u-{role}', password='test-pass-123')
            user.groups.add(Group.objects.get(name=role))
            cls.users[role] = user
        cls.superuser = User.objects.create_superuser(
            username='root', password='test-pass-123', email='root@example.com'
        )
        cls.ungrouped = User.objects.create_user(username='u-none', password='test-pass-123')
        cls.other_sales = User.objects.create_user(username='u-sales-2', password='test-pass-123')
        cls.other_sales.groups.add(Group.objects.get(name=permissions.SALES))

        # 最小 catalog 规格：4″ Class 150 WN
        cls.standard = Standard.objects.create(
            id='asme-b16-5', name='ASME B16.5', name_zh='美标管法兰',
            short='B16.5', edition='ASME B16.5-2025',
        )
        cls.type_wn = FlangeType.objects.create(
            slug='weld-neck', name_zh='对焊法兰', name_en='Weld Neck',
        )
        cls.rating_150 = Rating.objects.create(
            standard=cls.standard, code='150', label='Class 150',
        )
        cls.row = DimensionRow.objects.create(
            standard=cls.standard, flange_type=cls.type_wn, rating=cls.rating_150,
            size='4', size_label='4″', dn=100, od=228.6, th=22.4, rf=157.2,
            rf_height=1.5, bc=190.5, bolts=8, bolt_hole=19.1,
            hub=134.9, length=74.7, hub_weld=114.3, weight=6.8,
        )
        cls.material = Material.objects.create(
            code='A105', label='ASTM A105 / A105N', family='碳钢',
            density=Decimal('7.850'),
        )
        cls.price = MaterialPrice.objects.create(
            material=cls.material, price_per_kg=Decimal('7.20'),
            effective_from=date(2020, 1, 1), source='purchase',
        )

        cls.quote_own = Quotation.objects.create(
            number='FQ-9901-0001', created_by=cls.users[permissions.SALES],
            valid_until=date(2099, 12, 31), status=Quotation.Status.DRAFT,
        )
        cls.quote_other = Quotation.objects.create(
            number='FQ-9901-0002', created_by=cls.other_sales,
            valid_until=date(2099, 12, 31), status=Quotation.Status.DRAFT,
        )
        cls.quote_pending = Quotation.objects.create(
            number='FQ-9901-0003', created_by=cls.users[permissions.SALES],
            valid_until=date(2099, 12, 31), status=Quotation.Status.PENDING,
        )

    @property
    def sales(self):
        return self.users[permissions.SALES]

    def api_request(self, method, path, payload=None, user=None):
        factory = RequestFactory()
        if method == 'post':
            request = factory.post(
                path, data=json.dumps(payload or {}), content_type='application/json'
            )
        else:
            request = factory.get(path)
        request.user = user if user is not None else self.sales
        return request


class RoleResolutionTests(PermissionFixtureMixin, TestCase):
    def test_role_for_each_role(self):
        for role in permissions.ROLE_NAMES:
            self.assertEqual(permissions.role_for(self.users[role]), role)

    def test_superuser_is_quote_admin(self):
        self.assertEqual(permissions.role_for(self.superuser), permissions.QUOTE_ADMIN)

    def test_ungrouped_and_anonymous_have_no_role(self):
        self.assertIsNone(permissions.role_for(self.ungrouped))
        self.assertIsNone(permissions.role_for(AnonymousUser()))
        self.assertIsNone(permissions.role_for(None))

    def test_multi_group_user_gets_highest_privilege(self):
        self.users[permissions.SALES].groups.add(Group.objects.get(name=permissions.FINANCE))
        self.assertEqual(permissions.role_for(self.users[permissions.SALES]), permissions.FINANCE)

    def test_matrix_is_configured(self):
        self.assertTrue(permissions.roles_configured())
        self.assertFalse(permissions.legacy_mode())


class CapabilityTests(PermissionFixtureMixin, TestCase):
    def test_can_view_cost(self):
        expected = {
            permissions.SALES: False,
            permissions.ENGINEERING: True,
            permissions.FINANCE: True,
            permissions.SALES_MANAGER: True,
            permissions.QUOTE_ADMIN: True,
        }
        for role, can_view in expected.items():
            self.assertIs(permissions.can_view_cost(self.users[role]), can_view, msg=role)
        self.assertTrue(permissions.can_view_cost(self.superuser))
        self.assertFalse(permissions.can_view_cost(self.ungrouped))
        self.assertFalse(permissions.can_view_cost(AnonymousUser()))

    def test_can_edit_prices(self):
        expected = {
            permissions.SALES: False,
            permissions.ENGINEERING: False,
            permissions.FINANCE: True,
            permissions.SALES_MANAGER: False,
            permissions.QUOTE_ADMIN: True,
        }
        for role, can_edit in expected.items():
            self.assertIs(permissions.can_edit_prices(self.users[role]), can_edit, msg=role)

    def test_can_approve(self):
        expected = {
            permissions.SALES: False,
            permissions.ENGINEERING: False,
            permissions.FINANCE: False,
            permissions.SALES_MANAGER: True,
            permissions.QUOTE_ADMIN: True,
        }
        for role, can_approve in expected.items():
            self.assertIs(permissions.can_approve(self.users[role]), can_approve, msg=role)

    def test_can_create_quote(self):
        expected = {
            permissions.SALES: True,
            permissions.ENGINEERING: False,
            permissions.FINANCE: False,
            permissions.SALES_MANAGER: True,
            permissions.QUOTE_ADMIN: True,
        }
        for role, can_create in expected.items():
            self.assertIs(permissions.can_create_quote(self.users[role]), can_create, msg=role)

    def test_can_view_all_quotes(self):
        expected = {
            permissions.SALES: False,
            permissions.ENGINEERING: True,
            permissions.FINANCE: True,
            permissions.SALES_MANAGER: True,
            permissions.QUOTE_ADMIN: True,
        }
        for role, can_all in expected.items():
            self.assertIs(permissions.can_view_all_quotes(self.users[role]), can_all, msg=role)

    def test_can_view_quote_ownership(self):
        sales = self.users[permissions.SALES]
        self.assertTrue(permissions.can_view_quote(sales, self.quote_own))
        self.assertFalse(permissions.can_view_quote(sales, self.quote_other))
        for role in (permissions.ENGINEERING, permissions.FINANCE,
                     permissions.SALES_MANAGER, permissions.QUOTE_ADMIN):
            self.assertTrue(
                permissions.can_view_quote(self.users[role], self.quote_other), msg=role
            )
        self.assertFalse(permissions.can_view_quote(self.ungrouped, self.quote_own))
        self.assertFalse(permissions.can_view_quote(AnonymousUser(), self.quote_own))

    def test_can_transition_matrix(self):
        sales = self.users[permissions.SALES]
        manager = self.users[permissions.SALES_MANAGER]
        finance = self.users[permissions.FINANCE]
        # sales：本人草稿可提交，不能审批，不能动别人的单
        self.assertTrue(permissions.can_transition(sales, self.quote_own, 'submit'))
        self.assertFalse(permissions.can_transition(sales, self.quote_own, 'approve'))
        self.assertFalse(permissions.can_transition(sales, self.quote_other, 'submit'))
        # 经理：可审批
        self.assertTrue(permissions.can_transition(manager, self.quote_pending, 'approve'))
        self.assertTrue(permissions.can_transition(manager, self.quote_pending, 'reject'))
        # 财务/技术：只读
        self.assertFalse(permissions.can_transition(finance, self.quote_own, 'submit'))
        self.assertFalse(
            permissions.can_transition(self.users[permissions.ENGINEERING],
                                       self.quote_own, 'submit')
        )
        # 管理员/superuser：全部
        self.assertTrue(
            permissions.can_transition(self.users[permissions.QUOTE_ADMIN],
                                       self.quote_other, 'approve')
        )
        self.assertTrue(permissions.can_transition(self.superuser, self.quote_other, 'cancel'))
        # 未分组/未登录/未知动作
        self.assertFalse(permissions.can_transition(self.ungrouped, self.quote_own, 'submit'))
        self.assertFalse(permissions.can_transition(AnonymousUser(), self.quote_own, 'submit'))
        self.assertFalse(permissions.can_transition(sales, self.quote_own, 'nonsense'))


class DataScopeTests(PermissionFixtureMixin, TestCase):
    def test_sales_sees_only_own_quotes(self):
        visible = permissions.visible_quotations(self.users[permissions.SALES])
        ids = set(visible.values_list('pk', flat=True))
        self.assertIn(self.quote_own.pk, ids)
        self.assertIn(self.quote_pending.pk, ids)
        self.assertNotIn(self.quote_other.pk, ids)

    def test_all_quote_roles_see_everything(self):
        for role in (permissions.ENGINEERING, permissions.FINANCE,
                     permissions.SALES_MANAGER, permissions.QUOTE_ADMIN):
            ids = set(
                permissions.visible_quotations(self.users[role]).values_list('pk', flat=True)
            )
            self.assertIn(self.quote_own.pk, ids, msg=role)
            self.assertIn(self.quote_other.pk, ids, msg=role)

    def test_ungrouped_and_anonymous_see_nothing(self):
        self.assertEqual(permissions.visible_quotations(self.ungrouped).count(), 0)
        self.assertEqual(permissions.visible_quotations(AnonymousUser()).count(), 0)

    def test_services_helper_alias(self):
        ids = set(
            services.quotations_for_user(self.users[permissions.SALES])
            .values_list('pk', flat=True)
        )
        self.assertIn(self.quote_own.pk, ids)
        self.assertNotIn(self.quote_other.pk, ids)

    def test_scope_label(self):
        self.assertEqual(permissions.quote_scope_label(self.users[permissions.SALES]), 'own')
        self.assertEqual(
            permissions.quote_scope_label(self.users[permissions.SALES_MANAGER]), 'all'
        )
        self.assertEqual(permissions.quote_scope_label(AnonymousUser()), 'none')


class ApiPermissionTests(PermissionFixtureMixin, TestCase):
    def _calc(self, user):
        return api.api_calc(self.api_request('post', '/quote/calc/', make_payload(), user=user))

    def test_sales_calc_redacts_cost_but_keeps_price(self):
        response = self._calc(self.users[permissions.SALES])
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertNotIn('costs', data)
        self.assertEqual(data['price']['unit_gross'], 170.55)
        self.assertEqual(data['price']['margin_pct'], 8.9)
        self.assertEqual(data['validation']['status'], 'pass')
        # 快照里也不能残留成本参数/材料单价/价格版本
        params = data['snapshot']['params']
        for key in ('material_price_per_kg', 'material_price_effective_from',
                    'blank_weight', 'machining_base', 'management_pct', 'yield'):
            self.assertNotIn(key, params)
        self.assertEqual(data['snapshot']['price_versions'], {})
        self.assertTrue(data['snapshot']['params']['demo_rates'])
        self.assertEqual(data['item']['net_weight'], 6.8)

    def test_cost_roles_see_full_costs(self):
        for role in (permissions.ENGINEERING, permissions.FINANCE,
                     permissions.SALES_MANAGER, permissions.QUOTE_ADMIN):
            data = json.loads(self._calc(self.users[role]).content)
            self.assertEqual(data['costs']['total'], 137.51, msg=role)
            self.assertEqual(data['snapshot']['params']['material_price_per_kg'], 7.2)

    def test_superuser_sees_cost(self):
        data = json.loads(self._calc(self.superuser).content)
        self.assertEqual(data['costs']['total'], 137.51)

    def test_ungrouped_calc_redacted_after_seed(self):
        data = json.loads(self._calc(self.ungrouped).content)
        self.assertNotIn('costs', data)

    def test_sales_options_hides_cost_rates(self):
        response = api.api_options(self.api_request('get', '/quote/options/'))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertNotIn('rate_table', data)
        self.assertNotIn('yield', data['defaults'])
        self.assertNotIn('rate', data['surfaces'][0])
        self.assertNotIn('fee', data['packaging'][0])

    def test_engineering_options_keeps_cost_rates(self):
        response = api.api_options(
            self.api_request('get', '/quote/options/',
                             user=self.users[permissions.ENGINEERING])
        )
        data = json.loads(response.content)
        self.assertIn('rate_table', data)
        self.assertIn('yield', data['defaults'])

    def test_anonymous_calc_403(self):
        response = self._calc(AnonymousUser())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)['error'], 'auth_required')

    def test_sales_can_create_and_response_hides_material_price(self):
        response = api.api_create(
            self.api_request('post', '/quote/', make_payload())
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertRegex(data['number'], r'^FQ-\d{4}-\d{4}$')
        self.assertNotIn('material_price', data['quote']['items'][0])
        self.assertIn('unit_price', data['quote']['items'][0])

    def test_read_only_roles_cannot_create(self):
        for role in (permissions.ENGINEERING, permissions.FINANCE):
            response = api.api_create(
                self.api_request('post', '/quote/', make_payload(),
                                 user=self.users[role])
            )
            self.assertEqual(response.status_code, 403, msg=role)
            self.assertEqual(json.loads(response.content)['error'], 'permission_denied')

    def test_manager_can_create(self):
        response = api.api_create(
            self.api_request('post', '/quote/', make_payload(),
                             user=self.users[permissions.SALES_MANAGER])
        )
        self.assertEqual(response.status_code, 200)

    def test_sales_submit_own_quote(self):
        response = api.api_status(
            self.api_request('post', f'/quote/{self.quote_own.number}/status/',
                             {'action': 'submit'}),
            self.quote_own.number,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['status'], Quotation.Status.PENDING)
        self.quote_own.refresh_from_db()
        self.assertEqual(self.quote_own.status, Quotation.Status.PENDING)

    def test_sales_cannot_touch_other_quote(self):
        response = api.api_status(
            self.api_request('post', f'/quote/{self.quote_other.number}/status/',
                             {'action': 'submit'}),
            self.quote_other.number,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)['error'], 'permission_denied')

    def test_engineering_cannot_transition(self):
        response = api.api_status(
            self.api_request('post', f'/quote/{self.quote_own.number}/status/',
                             {'action': 'submit'}, user=self.users[permissions.ENGINEERING]),
            self.quote_own.number,
        )
        self.assertEqual(response.status_code, 403)

    def test_sales_cannot_approve(self):
        response = api.api_status(
            self.api_request('post', f'/quote/{self.quote_pending.number}/status/',
                             {'action': 'approve'}),
            self.quote_pending.number,
        )
        self.assertEqual(response.status_code, 403)

    def test_manager_can_approve(self):
        response = api.api_status(
            self.api_request('post', f'/quote/{self.quote_pending.number}/status/',
                             {'action': 'approve'},
                             user=self.users[permissions.SALES_MANAGER]),
            self.quote_pending.number,
        )
        self.assertEqual(response.status_code, 200)
        self.quote_pending.refresh_from_db()
        self.assertEqual(self.quote_pending.status, Quotation.Status.APPROVED)
        self.assertEqual(
            self.quote_pending.approved_by_id,
            self.users[permissions.SALES_MANAGER].pk,
        )

    def test_status_api_requires_login(self):
        response = api.api_status(
            self.api_request('post', f'/quote/{self.quote_own.number}/status/',
                             {'action': 'submit'}, user=AnonymousUser()),
            self.quote_own.number,
        )
        self.assertEqual(response.status_code, 403)


class SeedRolesCommandTests(PermissionFixtureMixin, TestCase):
    def test_seed_roles_idempotent(self):
        self.assertEqual(
            Group.objects.filter(name__in=permissions.ROLE_NAMES).count(), 5
        )
        call_command('seed_roles', verbosity=0)
        call_command('seed_roles', verbosity=0)
        self.assertEqual(
            Group.objects.filter(name__in=permissions.ROLE_NAMES).count(), 5
        )

    def test_seed_roles_permission_matrix(self):
        sales = Group.objects.get(name=permissions.SALES)
        self.assertFalse(sales.permissions.filter(codename='change_materialprice').exists())
        finance = Group.objects.get(name=permissions.FINANCE)
        self.assertTrue(finance.permissions.filter(codename='change_materialprice').exists())
        self.assertTrue(finance.permissions.filter(codename='add_materialprice').exists())
        manager = Group.objects.get(name=permissions.SALES_MANAGER)
        self.assertFalse(manager.permissions.filter(codename='change_materialprice').exists())
        self.assertTrue(manager.permissions.filter(codename='change_quotation').exists())
        engineering = Group.objects.get(name=permissions.ENGINEERING)
        self.assertTrue(engineering.permissions.filter(codename='view_costsnapshot').exists())
        admin = Group.objects.get(name=permissions.QUOTE_ADMIN)
        quote_codenames = set(
            Permission.objects.filter(content_type__app_label='quote')
            .values_list('codename', flat=True)
        )
        admin_codenames = set(
            admin.permissions.filter(content_type__app_label='quote')
            .values_list('codename', flat=True)
        )
        self.assertEqual(admin_codenames, quote_codenames)

    def test_dry_run_does_not_write(self):
        before = Group.objects.filter(name__in=permissions.ROLE_NAMES).count()
        call_command('seed_roles', dry_run=True, verbosity=0)
        self.assertEqual(
            Group.objects.filter(name__in=permissions.ROLE_NAMES).count(), before
        )
