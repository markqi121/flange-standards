"""幂等创建报价权限角色组：sales / engineering / finance / sales_manager / quote_admin。

用法::

    python manage.py seed_roles            # 创建/收敛 5 个组及权限（幂等）
    python manage.py seed_roles --dry-run  # 只打印将要授予的权限

上线必跑：``seed_roles`` 之后 ``quote.permissions`` 的兼容模式自动关闭，
未加入角色组的用户将按"无权限"处理（看不到成本、不能建单/审批）。

注意：能力判定以 **组名** 为准（quote.permissions），这里的 Django Permission
主要服务于 /admin/ 后台的可操作性，两者保持一致。
"""

from __future__ import annotations

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from quote.permissions import (
    ENGINEERING,
    FINANCE,
    QUOTE_ADMIN,
    ROLE_LABELS,
    ROLE_NAMES,
    SALES,
    SALES_MANAGER,
)

# 每个角色需要授予的权限规格：
# - quote_all: quote 应用全部权限
# - quote_view/quote_add/quote_change: quote 应用指定模型的对应权限
# - price_edit: 价格库（MaterialPrice / ProcessRate）读写删
# - catalog_view: catalog 应用只读（标准库只读）
# - auth_view: 用户/组只读（quote_admin 的"用户权限"）
ROLE_SPECS = {
    SALES: {
        'quote_view': ['quotation', 'quotationitem', 'customer', 'material'],
        'quote_add': ['quotation', 'quotationitem'],
        'quote_change': ['quotation', 'quotationitem'],
        'catalog_view': True,
    },
    ENGINEERING: {
        'quote_view': [
            'quotation', 'quotationitem', 'costsnapshot', 'material',
            'materialprice', 'processrate', 'approvallog',
        ],
        'catalog_view': True,
    },
    FINANCE: {
        'quote_view': [
            'quotation', 'quotationitem', 'costsnapshot', 'customer',
            'material', 'approvallog',
        ],
        'price_edit': True,
        'catalog_view': True,
    },
    SALES_MANAGER: {
        'quote_view': [
            'quotation', 'quotationitem', 'costsnapshot', 'customer',
            'material', 'approvallog',
        ],
        'quote_add': ['quotation', 'quotationitem'],
        'quote_change': ['quotation', 'quotationitem'],
        'catalog_view': True,
    },
    QUOTE_ADMIN: {
        'quote_all': True,
        'price_edit': True,
        'catalog_view': True,
        'auth_view': True,
    },
}


def _quote_permissions(**filters):
    return set(Permission.objects.filter(content_type__app_label='quote', **filters))


def _model_permissions(prefix: str, models) -> set:
    perms = set()
    for model in models:
        perms |= set(
            Permission.objects.filter(
                content_type__app_label='quote',
                content_type__model=model,
                codename=f'{prefix}_{model}',
            )
        )
    return perms


def permissions_for_role(role: str) -> set:
    """返回某角色应收敛到的权限集合（不落库，便于 --dry-run/测试）。"""
    spec = ROLE_SPECS.get(role, {})
    perms = set()
    if spec.get('quote_all'):
        perms |= _quote_permissions()
    if spec.get('quote_view'):
        perms |= _model_permissions('view', spec['quote_view'])
    if spec.get('quote_add'):
        perms |= _model_permissions('add', spec['quote_add'])
    if spec.get('quote_change'):
        perms |= _model_permissions('change', spec['quote_change'])
    if spec.get('price_edit'):
        perms |= _model_permissions('add', ['materialprice', 'processrate'])
        perms |= _model_permissions('change', ['materialprice', 'processrate'])
        perms |= _model_permissions('delete', ['materialprice', 'processrate'])
        perms |= _model_permissions('view', ['materialprice', 'processrate'])
    if spec.get('catalog_view'):
        perms |= set(
            Permission.objects.filter(
                content_type__app_label='catalog', codename__startswith='view_'
            )
        )
    if spec.get('auth_view'):
        perms |= set(
            Permission.objects.filter(
                content_type__app_label='auth',
                content_type__model__in=['user', 'group'],
                codename__startswith='view_',
            )
        )
    return perms


class Command(BaseCommand):
    help = '幂等创建报价权限角色组（sales/engineering/finance/sales_manager/quote_admin）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true', help='只打印，不写数据库'
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        self.stdout.write('报价权限矩阵：%s' % '、'.join(
            f'{name}({ROLE_LABELS[name]})' for name in ROLE_NAMES
        ))
        for role in ROLE_NAMES:
            perms = permissions_for_role(role)
            group = None if dry_run else Group.objects.filter(name=role).first()
            created = group is None and not dry_run
            if not dry_run:
                group, created = Group.objects.get_or_create(name=role)
                group.permissions.set(perms)
            self.stdout.write(
                '  [%s] %s · %d 项权限%s'
                % (
                    '新增' if created else '更新',
                    role,
                    len(perms),
                    '（dry-run，未写入）' if dry_run else '',
                )
            )
        if dry_run:
            self.stdout.write(self.style.WARNING('dry-run：数据库未变更'))
        else:
            self.stdout.write(self.style.SUCCESS(
                '完成：%d 个角色组已就绪（幂等，可重复执行）' % len(ROLE_NAMES)
            ))
