"""报价权限矩阵：角色识别与能力判定（T7）。

5 个 Django Group 对应 5 个角色（契约 §5）：

============  ==============================================================
角色          能做什么
============  ==============================================================
sales         建/改本人草稿、查看本人报价与毛利；**不看成本明细**、不改价格库
engineering   只读全部报价，可看成本/重量/工艺参数；不能建单/审批/改价
finance       只读报价 + 价格库读写 + 看成本/毛利；不能建单/审批
sales_manager 全部报价、审批（approve/reject）、折扣；不改价格库
quote_admin   全部权限（含价格库、用户权限）；superuser 等价
============  ==============================================================

兼容模式（重要）
----------------
T7 之前的站点只有"登录即可用"。为了不让已上线环境/UI 测试在 `seed_roles`
执行前直接 403，本模块在**数据库中一个角色组都不存在**时启用兼容模式：
- 能力判定退回 T7 之前的行为（登录用户可用、可看成本）；
- 一旦执行过 ``python manage.py seed_roles``（生产上线必做），兼容模式关闭，
  未加入任何角色组的用户按"无权限"处理（看不到成本、不能建单/审批）。

所有 API 视图必须走本模块的能力函数，不要在视图里硬编码角色名。
"""

from __future__ import annotations

from typing import Optional

from django.contrib.auth.models import Group

SALES = 'sales'
ENGINEERING = 'engineering'
FINANCE = 'finance'
SALES_MANAGER = 'sales_manager'
QUOTE_ADMIN = 'quote_admin'

ROLE_NAMES = (SALES, ENGINEERING, FINANCE, SALES_MANAGER, QUOTE_ADMIN)
ROLE_LABELS = {
    SALES: '销售',
    ENGINEERING: '技术',
    FINANCE: '财务',
    SALES_MANAGER: '销售经理',
    QUOTE_ADMIN: '报价管理员',
}

# 多组用户按最高权限角色生效
ROLE_PRIORITY = (QUOTE_ADMIN, SALES_MANAGER, FINANCE, ENGINEERING, SALES)

COST_ROLES = frozenset({ENGINEERING, FINANCE, SALES_MANAGER, QUOTE_ADMIN})
PRICE_EDIT_ROLES = frozenset({FINANCE, QUOTE_ADMIN})
APPROVE_ROLES = frozenset({SALES_MANAGER, QUOTE_ADMIN})
ALL_QUOTES_ROLES = frozenset({ENGINEERING, FINANCE, SALES_MANAGER, QUOTE_ADMIN})
CREATE_ROLES = frozenset({SALES, SALES_MANAGER, QUOTE_ADMIN})
READ_ONLY_ROLES = frozenset({ENGINEERING, FINANCE})

# 状态机动作别名（与 services.transition 保持一致；此处不 import services 避免循环）
ACTION_ALIASES = {
    'pending_approval': 'submit',
    'pending': 'submit',
    'submit': 'submit',
    'approve': 'approve',
    'reject': 'reject',
    'send': 'send',
    'accept': 'accept',
    'order': 'order',
    'expire': 'expire',
    'cancel': 'cancel',
}

# sales 可执行的动作（本人报价）
SALES_ACTIONS = frozenset({'submit', 'send', 'accept', 'order', 'cancel', 'expire'})
MANAGER_ACTIONS = frozenset(
    {'submit', 'send', 'accept', 'order', 'cancel', 'expire', 'approve', 'reject'}
)


# ---------------------------------------------------------------- 角色识别
def is_authenticated(user) -> bool:
    return bool(user is not None and getattr(user, 'is_authenticated', False))


def role_for(user) -> Optional[str]:
    """返回用户角色名；未登录/无角色返回 None。superuser 视为 quote_admin。"""
    if not is_authenticated(user):
        return None
    if getattr(user, 'is_superuser', False):
        return QUOTE_ADMIN
    group_names = set(user.groups.values_list('name', flat=True))
    for role in ROLE_PRIORITY:
        if role in group_names:
            return role
    return None


def roles_configured() -> bool:
    """权限矩阵是否已初始化（数据库里是否存在任一角色组）。"""
    return Group.objects.filter(name__in=ROLE_NAMES).exists()


def legacy_mode() -> bool:
    """兼容模式：一个角色组都没有（尚未执行 seed_roles）。"""
    return not roles_configured()


def _legacy_allow(user) -> bool:
    return is_authenticated(user) and role_for(user) is None and legacy_mode()


# ---------------------------------------------------------------- 能力判定
def can_view_cost(user) -> bool:
    """能否查看成本明细（材料费/加工费/管理费/费率等）。"""
    if not is_authenticated(user):
        return False
    role = role_for(user)
    if role in COST_ROLES:
        return True
    return _legacy_allow(user)


def can_edit_prices(user) -> bool:
    """能否维护材料价/工艺费率（价格库）。"""
    return role_for(user) in PRICE_EDIT_ROLES


def can_approve(user) -> bool:
    """能否审批（approve/reject）。"""
    if not is_authenticated(user):
        return False
    role = role_for(user)
    if role in APPROVE_ROLES:
        return True
    return _legacy_allow(user)


def can_create_quote(user) -> bool:
    """能否创建报价单；engineering/finance 只读。"""
    if not is_authenticated(user):
        return False
    role = role_for(user)
    if role in CREATE_ROLES:
        return True
    return _legacy_allow(user)


def can_view_all_quotes(user) -> bool:
    """能否查看全部报价（销售只能看本人）。"""
    if not is_authenticated(user):
        return False
    role = role_for(user)
    if role in ALL_QUOTES_ROLES:
        return True
    return _legacy_allow(user)


def can_view_quote(user, quote) -> bool:
    """能否查看指定报价：sales 仅本人创建；技术/财务/经理/管理员全部。"""
    if quote is None or not is_authenticated(user):
        return False
    role = role_for(user)
    if role is None:
        return _legacy_allow(user)
    if role in ALL_QUOTES_ROLES:
        return True
    if role == SALES:
        return getattr(quote, 'created_by_id', None) == getattr(user, 'pk', None)
    return False


def can_transition(user, quote, action: str) -> bool:
    """能否对指定报价执行状态动作（submit/approve/reject/send/accept/order/expire/cancel）。

    - 具体状态是否允许由 ``services.transition`` 再校验（本函数只管角色/归属）；
    - engineering/finance 只读，不能流转；
    - sales 只能流转本人报价，且不能审批。
    """
    if quote is None or not is_authenticated(user):
        return False
    action_key = ACTION_ALIASES.get(str(action or '').strip().lower())
    if action_key is None:
        return False
    role = role_for(user)
    if role is None:
        return _legacy_allow(user)
    if role == QUOTE_ADMIN:
        return True
    if role == SALES_MANAGER:
        return action_key in MANAGER_ACTIONS
    if role == SALES:
        if action_key == 'reject':
            return can_view_quote(user, quote) and quote.status in {'pending', 'sent'}
        if action_key == 'approve':
            return False
        return action_key in SALES_ACTIONS and can_view_quote(user, quote)
    return False  # engineering / finance 只读


# ---------------------------------------------------------------- 数据范围
def visible_quotations(user):
    """按角色返回可见报价单 QuerySet。

    - sales：本人创建；
    - engineering/finance/sales_manager/quote_admin：全部；
    - 未登录/未分组（矩阵已初始化）：none。
    """
    from .models import Quotation

    if not is_authenticated(user):
        return Quotation.objects.none()
    role = role_for(user)
    if role is None:
        return Quotation.objects.all() if legacy_mode() else Quotation.objects.none()
    if role in ALL_QUOTES_ROLES:
        return Quotation.objects.all()
    if role == SALES:
        return Quotation.objects.filter(created_by_id=getattr(user, 'pk', None))
    return Quotation.objects.none()


def quote_scope_label(user) -> str:
    """给 UI 显示的数据范围：all / own / none。"""
    if not is_authenticated(user):
        return 'none'
    role = role_for(user)
    if role is None:
        return 'all' if legacy_mode() else 'none'
    if role in ALL_QUOTES_ROLES:
        return 'all'
    if role == SALES:
        return 'own'
    return 'none'
