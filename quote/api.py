"""报价 JSON API（契约 §6/§8）。

由 web-ui/Lead 在 ``quote/urls.py`` 中挂载（本任务不改 urls.py）。可直接追加
``quote.api.urlpatterns``（或 ``path('', include('quote.api'))``）：

============================  ==========================  =============
路径                         视图                         URL name
============================  ==========================  =============
``/quote/options/``           ``api_options``             ``quote:api_options``
``/quote/calc/``              ``api_calc``                ``quote:api_calc``
``/quote/`` POST              ``api_create``              ``quote:api_create``
``/quote/<number>/status/``   ``api_status``              ``quote:api_status``
============================  ==========================  =============

注意：``<number>/status/`` 之前必须先注册静态路径（options/calc），避免被
``<str:number>/`` 吞掉。

约定：
- 先按“登录用户”鉴权（未登录返回 403 JSON），再按角色组做能力/数据范围校验
  （见 ``quote/permissions.py``；无成本权限者的 calc 结果会剥离 costs）；
- POST 保留 Django CSRF 中间件保护（不豁免）；
- 成功 ``{"ok": true, ...}``；失败 ``{"ok": false, "error", "message", "field"}``
  与 HTTP 400/403/404/409（契约 §8）。
"""

from __future__ import annotations

import json

from django.http import JsonResponse
from django.urls import path
from django.views.decorators.http import require_GET, require_POST

from . import costing, permissions, pricing, services, weight

URL_NAMES = {
    'options': 'quote:api_options',
    'calc': 'quote:api_calc',
    'create': 'quote:api_create',
    'status': 'quote:api_status',
}


def _json(payload, status=200):
    return JsonResponse(payload, status=status, json_dumps_params={'ensure_ascii': False})


def _error(error, message, field=None, status=400):
    return _json({'ok': False, 'error': error, 'message': str(message), 'field': field}, status=status)


# 无成本权限角色（sales）可见的快照参数白名单：其余成本字段一律剥离
_SALES_SAFE_SNAPSHOT_KEYS = (
    'currency', 'fx_rate', 'tax_pct', 'profit_pct', 'discount_pct',
    'design_temp_c', 'design_pressure_bar', 'surface', 'packaging',
    'inspections', 'quantity_discount_pct', 'lead_time_days',
    'demo_rates', 'rate_source', 'weight_rule',
)


def _redact_options(data: dict) -> dict:
    """剥离级联选项里的成本参数/费率（sales 仍可拿到名称用于选型）。"""
    redacted = dict(data)
    redacted.pop('rate_table', None)
    defaults = dict(redacted.get('defaults') or {})
    for key in ('yield', 'machining_factor', 'management_pct'):
        defaults.pop(key, None)
    redacted['defaults'] = defaults
    for key, cost_fields in (
        ('surfaces', ('rate',)),
        ('packaging', ('fee',)),
        ('inspections', ('fee',)),
    ):
        redacted[key] = [
            {k: v for k, v in item.items() if k not in cost_fields}
            for item in (redacted.get(key) or [])
        ]
    return redacted


def _redact_cost(result: dict) -> dict:
    """剥离成本明细，只保留报价结论（price/margin/validation）。

    除顶层 ``costs`` 外，还要清掉 snapshot 里的材料单价、成本参数与价格版本，
    避免通过计算接口变相读取成本。
    """
    redacted = dict(result)
    redacted.pop('costs', None)
    snapshot = dict(redacted.get('snapshot') or {})
    params = snapshot.get('params') or {}
    snapshot['params'] = {
        key: params[key] for key in _SALES_SAFE_SNAPSHOT_KEYS if key in params
    }
    snapshot['price_versions'] = {}
    redacted['snapshot'] = snapshot
    return redacted


def login_required_json(view):
    """未登录返回 403 JSON（契约要求 /quote/* 对未登录不可用）。"""

    def wrapper(request, *args, **kwargs):
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return _error('auth_required', '请先登录', status=403)
        return view(request, *args, **kwargs)

    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def _parse_json_body(request) -> dict:
    raw = request.body or b''
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise services.InvalidPayload('请求体不是合法 JSON') from None
    if not isinstance(payload, dict):
        raise services.InvalidPayload('请求体必须是 JSON 对象')
    return payload


def _handle_exception(exc):
    """业务异常 → 契约 §8 错误 JSON。"""
    if isinstance(exc, services.QuoteError):
        return _error(exc.code, exc.message, field=exc.field, status=exc.http_status)
    if isinstance(exc, weight.WeightError):
        return _error('weight_unavailable', exc, field='size')
    if isinstance(exc, pricing.PriceError):
        return _error('price_unavailable', exc)
    if isinstance(exc, costing.CostingError):
        return _error('invalid_payload', exc, field='cost')
    raise exc


@require_GET
@login_required_json
def api_options(request):
    """级联选项：标准/类型/等级/口径/材质/工艺/包装/检测/默认参数。"""
    try:
        data = services.catalog_options()
    except Exception as exc:  # noqa: BLE001 - 统一转 JSON 错误
        return _handle_exception(exc)
    if not permissions.can_view_cost(request.user):
        data = _redact_options(data)
    payload = {'ok': True}
    payload.update(data)
    return _json(payload)


@require_POST
@login_required_json
def api_calc(request):
    """契约 §3.2：计算单条报价（不落库）。"""
    try:
        body = _parse_json_body(request)
        result = services.calculate(body, user=request.user)
    except Exception as exc:  # noqa: BLE001
        return _handle_exception(exc)
    if not permissions.can_view_cost(request.user):
        result = _redact_cost(result)
    payload = {'ok': True}
    payload.update(result)
    return _json(payload)


@require_POST
@login_required_json
def api_create(request):
    """契约 §3.3：创建报价单（单条或多条明细）。"""
    if not permissions.can_create_quote(request.user):
        return _error('permission_denied', '当前角色不能创建报价单', status=403)
    try:
        body = _parse_json_body(request)
        quote = services.create_quote(body, request.user)
    except Exception as exc:  # noqa: BLE001
        return _handle_exception(exc)
    return _json({
        'ok': True,
        'number': quote.number,
        'status': quote.status,
        'version': quote.version,
        'quote': services.quote_to_dict(
            quote, include_cost=permissions.can_view_cost(request.user)
        ),
    })


@require_POST
@login_required_json
def api_status(request, number):
    """契约 §4：状态流转（submit/approve/reject/send/accept/order/expire/cancel）。"""
    try:
        body = _parse_json_body(request)
        action = body.get('action') or body.get('to')
        if not action:
            raise services.InvalidPayload('缺少 action', field='action')
        quote = services.get_quote(number)
        if not permissions.can_transition(request.user, quote, action):
            return _error('permission_denied', '当前角色不能执行该状态操作', status=403)
        quote = services.transition(quote, action, request.user, body.get('comment') or '')
    except Exception as exc:  # noqa: BLE001
        return _handle_exception(exc)
    return _json({
        'ok': True,
        'number': quote.number,
        'status': quote.status,
        'version': quote.version,
        'quote': services.quote_to_dict(
            quote, include_cost=permissions.can_view_cost(request.user)
        ),
    })


# 供 `include('quote.api')` 或追加到 quote/urls.py；顺序不可调换：
# 静态 options/calc 必须在 <str:number>/ 之前。
urlpatterns = [
    path('options/', api_options, name='api_options'),
    path('calc/', api_calc, name='api_calc'),
    path('', api_create, name='api_create'),
    path('<str:number>/status/', api_status, name='api_status'),
]
