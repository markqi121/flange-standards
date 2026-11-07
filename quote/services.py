"""报价服务层：编号、计算、创建、版本、状态机、成本快照。

契约对应关系（``docs/报价功能-接口契约.md``）：

- §3.1 重量：``quote.weight.resolve_weight``；
- §3.2 计算：``calculate(input, user=None) -> dict``；
- §3.3 报价单：``next_quote_number / create_quote / transition``（+ 版本 create_version）；
- §3.4 价格：``quote.pricing.price_for / rate_for``；
- §4   状态机：``transition``；
- §2.6 CostSnapshot：创建报价时冻结参数与价格生效日期。

约定：
- 所有金额 Decimal，输出转 float 供 JSON；重量 3 位、金额 2 位；
- 计算与创建使用同一套 ``_compute``，保证"算出来的"和"存下来的"一致；
- 报价编号 ``FQ-YYMM-####`` 按月递增，唯一约束 + 重试兜底。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional

from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Max
from django.utils import timezone

from catalog.models import DimensionRow, FlangeType, Standard

from . import costing, pricing, validation, weight
from .models import (
    DEFAULT_WEIGHT_RULE,
    ApprovalLog,
    CostSnapshot,
    Customer,
    Material,
    MaterialPrice,
    ProcessRate,
    Quotation,
    QuotationItem,
)

QUOTE_NUMBER_PREFIX = 'FQ'
QUOTE_NUMBER_SEQ_MAX = 9999

DATE_FORMATS = ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d')


# ---------------------------------------------------------------- 异常
class QuoteError(Exception):
    """业务异常基类；api 层据此转 400/404/409 JSON。"""

    code = 'quote_error'
    http_status = 400

    def __init__(self, message: str, *, field: Optional[str] = None,
                 code: Optional[str] = None, http_status: Optional[int] = None):
        super().__init__(message)
        self.message = str(message)
        self.field = field
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status


class InvalidPayload(QuoteError):
    code = 'invalid_payload'


class InvalidCombination(QuoteError):
    code = 'invalid_combination'


class PriceUnavailable(QuoteError):
    code = 'price_unavailable'


class WeightUnavailable(QuoteError):
    code = 'weight_unavailable'


class ValidationFailed(QuoteError):
    code = 'validation_failed'


class QuoteNotFound(QuoteError):
    code = 'not_found'
    http_status = 404


class InvalidTransition(QuoteError):
    code = 'invalid_transition'
    http_status = 409


class PermissionDenied(QuoteError):
    """角色无权执行该操作（API 层转 403 JSON）。"""

    code = 'permission_denied'
    http_status = 403


# ---------------------------------------------------------------- 工具
def _dec(value, default=Decimal('0')) -> Decimal:
    if value is None or value == '':
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        raise InvalidPayload(f'无法解析为数字：{value!r}') from None


def _parse_date(value) -> Optional[date]:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise InvalidPayload(f'日期格式应为 YYYY-MM-DD：{value!r}', field='valid_until')


def _int(value, default=1) -> int:
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidPayload(f'数量必须是整数：{value!r}', field='qty') from None


def _require(payload: dict, key: str, field: Optional[str] = None):
    value = payload.get(key)
    if value is None or value == '':
        raise InvalidPayload(f'缺少必填字段 {key}', field=field or key)
    return value


def _json_safe(value):
    """把 Decimal 递归转成 float，便于 JsonResponse。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


# ---------------------------------------------------------------- 参数
def _resolve_params(payload: dict) -> dict:
    """合并契约 §7 默认参数 + 顶层便利字段 + payload['params']，并做范围校验。"""
    params = {
        'yield': Decimal(costing.DEFAULT_YIELD),
        'machining_factor': Decimal(costing.DEFAULT_MACHINING_FACTOR),
        'management_pct': Decimal(costing.DEFAULT_MANAGEMENT_PCT),
        'profit_pct': Decimal(pricing.DEFAULT_PARAMS['profit_pct']),
        'discount_pct': Decimal(pricing.DEFAULT_PARAMS['discount_pct']),
        'tax_pct': Decimal(pricing.DEFAULT_PARAMS['tax_pct']),
        'fx_rate': Decimal(pricing.DEFAULT_PARAMS['fx_rate']),
        'currency': pricing.DEFAULT_PARAMS['currency'],
        'freight_per_kg': Decimal(costing.DEFAULT_FREIGHT_PER_KG),
        # 动态成本参数（本次报价手工覆盖；None = 用价格库/默认值）
        'material_price_per_kg': None,
        'machining_rate': None,
    }
    # 顶层便利字段（契约 §3.2 的 discount_pct / currency / fx_rate 在顶层）
    top_mapping = {
        'discount_pct': 'discount_pct',
        'profit_pct': 'profit_pct',
        'tax_pct': 'tax_pct',
        'management_pct': 'management_pct',
        'freight_per_kg': 'freight_per_kg',
        'fx_rate': 'fx_rate',
        'currency': 'currency',
        'machining_factor': 'machining_factor',
        'yield': 'yield',
    }
    for key, param_key in top_mapping.items():
        if payload.get(key) is not None and payload.get(key) != '':
            params[param_key] = payload[key]

    nested = payload.get('params') or {}
    if not isinstance(nested, dict):
        raise InvalidPayload('params 必须是对象', field='params')
    params.update(nested)

    for key in ('yield', 'machining_factor', 'management_pct', 'profit_pct',
                'discount_pct', 'tax_pct', 'fx_rate', 'freight_per_kg'):
        if key == 'currency':
            continue
        params[key] = _dec(params.get(key))
    params['currency'] = str(params.get('currency') or 'CNY').upper()

    # 动态成本参数：本次材料价（>0）/ 工资率（>=0）
    for dynamic_key in ('material_price_per_kg', 'machining_rate'):
        raw = params.get(dynamic_key)
        if raw is None or raw == '':
            params[dynamic_key] = None
        else:
            params[dynamic_key] = _dec(raw)
    if params['material_price_per_kg'] is not None and params['material_price_per_kg'] <= 0:
        raise InvalidPayload(
            '本次材料价 material_price_per_kg 必须大于 0', field='material_price_per_kg'
        )
    if params['machining_rate'] is not None and params['machining_rate'] < 0:
        raise InvalidPayload(
            '本次工资率 machining_rate 不能为负', field='machining_rate'
        )

    if not Decimal('0') < params['yield'] <= Decimal('1'):
        raise InvalidPayload('材料利用率 yield 必须在 (0, 1] 区间', field='yield')
    if params['machining_factor'] <= 0:
        raise InvalidPayload('加工系数 machining_factor 必须大于 0', field='machining_factor')
    if params['management_pct'] < 0:
        raise InvalidPayload('管理费率 management_pct 不能为负', field='management_pct')
    if params['discount_pct'] < 0 or params['discount_pct'] > Decimal('50'):
        raise InvalidPayload('折扣必须在 0–50% 之间', field='discount_pct')
    if params['tax_pct'] < 0 or params['tax_pct'] > Decimal('100'):
        raise InvalidPayload('税率必须在 0–100% 之间', field='tax_pct')
    if params['fx_rate'] <= 0:
        raise InvalidPayload('汇率 fx_rate 必须大于 0', field='fx_rate')
    if params['freight_per_kg'] < 0:
        raise InvalidPayload('运费单价不能为负', field='freight_per_kg')
    if params['currency'] not in {'CNY', 'USD'}:
        raise InvalidPayload(f'不支持的币种：{params["currency"]}', field='currency')
    return params


# ---------------------------------------------------------------- 数据取值
def _find_row(std: str, flange_type: str, size, rating: str) -> Optional[DimensionRow]:
    return (
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating')
        .filter(
            standard_id=std,
            flange_type_id=flange_type,
            size=str(size),
            rating__code=str(rating),
        )
        .first()
    )


def _find_material(code) -> Material:
    material = Material.objects.filter(code=str(code)).first()
    if material is None:
        raise InvalidPayload(f'材质不存在：{code}', field='material')
    if not material.active:
        raise InvalidPayload(f'材质已停用：{code}', field='material')
    return material


# ---------------------------------------------------------------- 演示价格识别
# 契约 ProcessRate.code：machining / surface / inspection / packaging / freight / mgmt
RATE_CODES = ('machining', 'surface', 'packaging', 'inspection', 'freight', 'mgmt')
# 只有实际勾选固定检测项时 inspection 费率才参与成本（third 用固定百分比常量）
FIXED_INSPECTION_CODES = ('pmi', 'ut', 'hydro', 'hardness')

DEMO_SOURCE = 'demo'
DEMO_LABEL_KEYWORD = '演示'
DEMO_DEFAULTS = 'demo_defaults'
DEMO_MIXED = 'demo_mixed'
PROCESS_RATE = 'process_rate'


def _is_demo_source(source) -> bool:
    return str(source or '').strip().lower() == DEMO_SOURCE


def _contains_demo_keyword(*texts) -> bool:
    return any(DEMO_LABEL_KEYWORD in str(text or '') for text in texts)


def is_demo_material_price(material_price) -> bool:
    """材料价是否演示来源：``source=demo`` 或备注含“演示”。"""
    if material_price is None:
        return False
    return _is_demo_source(getattr(material_price, 'source', '')) or _contains_demo_keyword(
        getattr(material_price, 'note', '')
    )


def is_demo_process_rate(process_rate) -> bool:
    """费率是否演示来源：``source=demo`` 或 label 含“演示”。"""
    if process_rate is None:
        return False
    return _is_demo_source(getattr(process_rate, 'source', '')) or _contains_demo_keyword(
        getattr(process_rate, 'label', '')
    )


def demo_provenance(material_price, rate_provenance: dict) -> dict:
    """汇总一次计算中演示价格/费率的参与情况（只标记，不阻断计算）。

    ``rate_source``：

    - ``process_rate``：所有参与计算的费率与材料价都是真实来源（非 demo）；
    - ``demo_mixed``：真实费率与 demo/未配置项混用，或材料价是 demo；
    - ``demo_defaults``：没有任何真实费率参与（全部 demo 或回退到 costing 演示默认值）。
    """
    rate_provenance = rate_provenance or {}
    material_demo = is_demo_material_price(material_price)
    used = set(rate_provenance.get('used_codes') or [])
    demo = set(rate_provenance.get('demo_codes') or [])
    defaults = set(rate_provenance.get('default_codes') or [])
    real = sorted(used - demo)

    demo_rates = bool(demo) or bool(defaults) or material_demo
    if not demo_rates:
        rate_source = PROCESS_RATE
    elif real:
        rate_source = DEMO_MIXED
    else:
        rate_source = DEMO_DEFAULTS
    return {
        'demo_rates': demo_rates,
        'rate_source': rate_source,
        'demo_rate_codes': sorted(demo),
        'default_rate_codes': sorted(defaults),
        'real_rate_codes': real,
        'demo_material_price': material_demo,
        'material_price_source': str(getattr(material_price, 'source', '') or ''),
    }


def _resolve_rates(on_date: date, inspections=()) -> tuple:
    """从 ProcessRate 价格库读取费率覆盖；返回 ``(kwargs, rate_versions, provenance)``。

    provenance 记录：哪些费率真正参与计算（used/default）、哪些是演示来源（demo）。
    金额仍是 Decimal；未配置的项回退到 costing 演示默认值，但会在快照里标成
    ``demo_rates=True`` / ``rate_source=demo_defaults``，不允许静默当真实费率。
    """
    from .pricing import PriceError, all_rates

    kwargs: Dict[str, object] = {}
    versions: Dict[str, str] = {}
    used_codes = set()
    demo_codes = set()
    try:
        rates = all_rates(on_date)
    except PriceError:
        rates = {}

    def _tier_decimal_map(raw):
        return {str(k): _dec(v) for k, v in (raw or {}).items()}

    def _take(code):
        rate = rates.get(code)
        if rate is None:
            return None
        used_codes.add(code)
        versions[code] = rate.effective_from.isoformat()
        if is_demo_process_rate(rate):
            demo_codes.add(code)
        return rate

    machining = _take('machining')
    if machining is not None:
        if isinstance(machining.tiers, dict) and machining.tiers:
            kwargs['machining_base_override'] = None
            kwargs['machining_tiers'] = [
                (int(limit), _dec(value)) for limit, value in machining.tiers.items()
            ]
        elif machining.rate > 0:
            kwargs['machining_base_override'] = machining.rate

    surface = _take('surface')
    if surface is not None:
        if isinstance(surface.tiers, dict) and surface.tiers:
            kwargs['surface_rates'] = {**costing.SURFACE_RATES, **_tier_decimal_map(surface.tiers)}

    packaging = _take('packaging')
    if packaging is not None:
        if isinstance(packaging.tiers, dict) and packaging.tiers:
            kwargs['packaging_fees'] = {**costing.PACKAGING_FEES, **_tier_decimal_map(packaging.tiers)}

    inspection = _take('inspection')
    if inspection is not None:
        if isinstance(inspection.tiers, dict) and inspection.tiers:
            kwargs['inspection_fees'] = {**costing.INSPECTION_FEES, **_tier_decimal_map(inspection.tiers)}

    freight = _take('freight')
    if freight is not None:
        kwargs['freight_per_kg'] = freight.rate

    management = _take('mgmt')
    if management is not None:
        kwargs['management_pct'] = management.rate

    # 参与计算的费率代码：inspection 只有在勾选了固定检测项时才算参与
    participating = set(RATE_CODES)
    try:
        selected = set(costing.normalize_inspections(inspections))
    except costing.CostingError:
        selected = set()
    if not selected & set(FIXED_INSPECTION_CODES):
        participating.discard('inspection')

    provenance = {
        'participating_codes': sorted(participating),
        'used_codes': sorted(used_codes & participating),
        'default_codes': sorted(participating - used_codes),
        'demo_codes': sorted(demo_codes & participating),
    }
    return kwargs, versions, provenance


# ---------------------------------------------------------------- 计算
@dataclass
class Calculation:
    output: dict
    row: DimensionRow
    material: Material
    material_price: Optional[MaterialPrice]
    weights: weight.WeightResult
    cost: costing.CostResult
    price: pricing.PriceResult
    validation: dict
    params: dict
    snapshot: dict
    rate_versions: dict = field(default_factory=dict)


def _pt_check(row, material, temp_c, pressure_bar) -> dict:
    """只有 ASME B16.5 接入了 p/T；其它标准显式返回 na。"""
    rating_code = row.rating.code
    if row.standard_id != 'asme-b16-5':
        group = validation.material_group(material.code)
        return {
            'status': validation.STATUS_NA,
            'allowed_bar': None,
            'material_group': group,
            'method': 'standard_not_connected',
            'rating': validation._normalize_rating(rating_code),
            'messages': ['该标准暂未接入压力–温度表；P1 按标准补充后启用校验。'],
        }
    if temp_c is None:
        temp_c = 38  # 默认环境温度，与前端一致
    return validation.validate_pt(material.code, rating_code, temp_c, pressure_bar)


def _build_snapshot(row, material, params, cost, price, rate_versions,
                    temp_c, pressure_bar, demo_info, price_info) -> dict:
    demo_info = demo_info or {}
    price_info = price_info or {}
    params_json = _json_safe({
        'yield': params['yield'],
        'machining_factor': params['machining_factor'],
        'management_pct': params['management_pct'],
        'profit_pct': params['profit_pct'],
        'discount_pct': params['discount_pct'],
        'tax_pct': params['tax_pct'],
        'fx_rate': params['fx_rate'],
        'currency': params['currency'],
        'freight_per_kg': params['freight_per_kg'],
        'design_temp_c': temp_c,
        'design_pressure_bar': pressure_bar,
        'surface': cost.surface_option,
        'packaging': cost.packaging_option,
        'inspections': list(cost.inspections),
        # 本次实际材料价 / 价格库基准价 / 来源（manual|library）/ 工资率来源
        'material_price_per_kg': price_info.get('material_price_actual'),
        'material_price_base': price_info.get('material_price_base'),
        # manual/library 来源：origin 为规范值；source 保留库价原来源（兼容历史断言）
        'material_price_origin': price_info.get('material_price_origin', 'library'),
        'material_price_source': price_info.get('material_price_source', ''),
        'material_price_library_source': price_info.get('material_price_library_source'),
        'material_price_effective_from': price_info.get('material_price_effective_from'),
        'machining_rate': price_info.get('machining_rate'),
        'machining_rate_source': price_info.get('machining_rate_source', 'default'),
        'demo_material_price': bool(demo_info.get('demo_material_price')),
        'quantity_discount_pct': price.quantity_discount_pct,
        'lead_time_days': cost.lead_time_days,
        # 演示价格/费率可识别性：任何 demo 参与都必须标 True，不能静默 process_rate
        'demo_rates': bool(demo_info.get('demo_rates')),
        'rate_source': demo_info.get('rate_source', PROCESS_RATE),
        'demo_rate_codes': list(demo_info.get('demo_rate_codes') or []),
        'default_rate_codes': list(demo_info.get('default_rate_codes') or []),
        'real_rate_codes': list(demo_info.get('real_rate_codes') or []),
        'rate_versions': rate_versions,
        'weight_rule': DEFAULT_WEIGHT_RULE,
    })
    return {
        'params': params_json,
        # 便利字段：与 params 同步，便于 API/QA 直接读取
        'demo_rates': params_json['demo_rates'],
        'rate_source': params_json['rate_source'],
        'standard_editions': {row.standard_id: row.standard.effective_edition},
        'price_versions': {
            'material_prices': {
                material.code: {
                    'effective_from': price_info.get('material_price_effective_from'),
                    'price_per_kg': (
                        float(price_info['material_price_base'])
                        if price_info.get('material_price_base') is not None else None
                    ),
                    'source': price_info.get('material_price_source', ''),
                    'actual_price_per_kg': float(price_info.get('material_price_actual') or 0),
                }
            },
            'process_rates': dict(rate_versions),
        },
        'weight_rule': DEFAULT_WEIGHT_RULE,
    }


def _ensure_manual_cost_allowed(user) -> None:
    """手工成本参数（材料价/工资率）只允许成本可见角色使用。

    - ``user=None`` 的内部调用不受影响（定时任务/命令行/内部计算）；
    - 未登录（AnonymousUser）同样视为内部调用；
    - 已登录但无 ``can_view_cost``（如 sales）→ 403 语义，防止绕过接口手工改成本。
    """
    if user is None:
        return
    if not getattr(user, 'is_authenticated', False):
        return
    from .permissions import can_view_cost

    if not can_view_cost(user):
        raise PermissionDenied(
            '当前角色不能使用手工材料价/工资率覆盖成本参数', field='params'
        )


def _compute(payload: dict, on_date: Optional[date] = None, user=None) -> Calculation:
    if not isinstance(payload, dict):
        raise InvalidPayload('请求体必须是 JSON 对象')

    on_date = on_date or _parse_date(payload.get('on_date')) or timezone.localdate()
    std = str(_require(payload, 'std'))
    flange_type = str(_require(payload, 'type', field='flange_type'))
    size = _require(payload, 'size')
    rating = _require(payload, 'rating')
    material_code = _require(payload, 'material')
    qty = _int(payload.get('qty', 1))

    row = _find_row(std, flange_type, size, rating)
    if row is None:
        raise InvalidCombination(
            f'该标准/类型/等级/口径组合不存在：{std} / {flange_type} / {rating} / {size}',
            field='size',
        )

    material = _find_material(material_code)
    params = _resolve_params(payload)

    material_price_override = params.get('material_price_per_kg')
    machining_rate_override = params.get('machining_rate')
    if material_price_override is not None or machining_rate_override is not None:
        _ensure_manual_cost_allowed(user)

    temp_c = payload.get('design_temp_c')
    if temp_c is not None and temp_c != '':
        temp_c = _dec(temp_c)
        temp_c = int(temp_c) if temp_c == temp_c.to_integral_value() else float(temp_c)
    else:
        temp_c = None
    pressure_bar = payload.get('design_pressure_bar')
    pressure_bar = None if pressure_bar in (None, '') else _dec(pressure_bar)

    surface = str(payload.get('surface') or 'none')
    packaging = str(payload.get('packaging') or 'wood')
    inspections = payload.get('inspections') or []

    # 重量
    try:
        weights = weight.resolve_weight(row, material)
    except weight.WeightError as exc:
        raise WeightUnavailable(str(exc), field='size') from exc

    # 材料价：库价始终读取作为基准/回退；本次手工价（params.material_price_per_kg）优先
    library_material_price = None
    try:
        library_material_price = pricing.price_for(material, on_date)
    except pricing.PriceError as exc:
        if material_price_override is None:
            raise PriceUnavailable(str(exc), field='material') from exc
    if material_price_override is not None:
        actual_material_price = material_price_override
        material_price_origin = 'manual'
    else:
        actual_material_price = _dec(library_material_price.price_per_kg)
        material_price_origin = 'library'

    rates_kwargs, rate_versions, rate_provenance = _resolve_rates(
        on_date, inspections=inspections
    )

    # 成本（价格库费率覆盖 params 中的同名项）
    cost_kwargs = {
        'net_weight': weights.net_weight,
        'dn': row.dn,
        'flange_type': row.flange_type_id,
        'qty': qty,
        'material_price': actual_material_price,
        'yield_pct': params['yield'],
        'machining_factor': params['machining_factor'],
        'management_pct': params['management_pct'],
        'surface': surface,
        'packaging': packaging,
        'inspections': inspections,
        'freight_per_kg': params['freight_per_kg'],
    }
    cost_kwargs.update(rates_kwargs)
    if machining_rate_override is not None:
        # 本次工资率覆盖 ProcessRate 档位/基础费率
        cost_kwargs['machining_base_override'] = machining_rate_override
        cost_kwargs.pop('machining_tiers', None)
    try:
        cost = costing.compute_cost(**cost_kwargs)
    except costing.CostingError as exc:
        raise InvalidPayload(str(exc), field='cost') from exc

    # 演示价格/费率识别（材料价 + 参与计算的费率；只标记，不阻断）
    demo_info = demo_provenance(
        None if material_price_origin == 'manual' else library_material_price,
        rate_provenance,
    )
    if machining_rate_override is not None:
        demo_info = dict(demo_info)
        for code_key in ('demo_rate_codes', 'default_rate_codes', 'real_rate_codes'):
            demo_info[code_key] = [
                code for code in (demo_info.get(code_key) or []) if code != 'machining'
            ]
        demo_info['rate_source'] = 'manual_override'
    cost.demo_rates = demo_info['demo_rates']

    used_rate_codes = set(rate_provenance.get('used_codes') or [])
    if machining_rate_override is not None:
        machining_rate_source = 'manual'
    elif 'machining' in used_rate_codes:
        machining_rate_source = 'process'
    else:
        machining_rate_source = 'default'
    price_info = {
        'material_price_actual': actual_material_price,
        'material_price_base': (
            _dec(library_material_price.price_per_kg) if library_material_price else None
        ),
        'material_price_origin': material_price_origin,
        'material_price_source': (
            'manual' if material_price_origin == 'manual'
            else str(getattr(library_material_price, 'source', '') or '')
        ),
        'material_price_library_source': (
            str(getattr(library_material_price, 'source', '') or '')
            if library_material_price else None
        ),
        'material_price_effective_from': (
            library_material_price.effective_from.isoformat()
            if library_material_price else None
        ),
        'machining_rate': cost.machining_base,
        'machining_rate_source': machining_rate_source,
    }

    # 定价
    try:
        price = pricing.compute_price(
            unit_cost=cost.total,
            qty=qty,
            profit_pct=params['profit_pct'],
            discount_pct=params['discount_pct'],
            tax_pct=params['tax_pct'],
            net_weight=weights.net_weight,
            currency=params['currency'],
            fx_rate=params['fx_rate'],
        )
    except pricing.PriceError as exc:
        raise InvalidPayload(str(exc), field='price') from exc

    # 校验
    pt = _pt_check(row, material, temp_c, pressure_bar)
    checks = [
        validation.validate_combination(
            standard_id=row.standard_id,
            flange_type_id=row.flange_type_id,
            size=row.size,
            rating_code=row.rating.code,
            row=row,
        ),
        validation.validate_qty(qty),
        validation.validate_discount(params['discount_pct']),
    ]
    val = validation.validate_all(pt=pt, checks=checks)

    needs_approval = pricing.approval_required(
        price.discount_pct, price.total_gross, price.margin_raw
    )
    snapshot = _build_snapshot(
        row, material, params, cost, price, rate_versions,
        temp_c, pressure_bar, demo_info, price_info,
    )

    output = {
        'item': {
            'description': row.title_zh,
            'standard_edition': row.standard.effective_edition,
            'material': material.label,
            'material_code': material.code,
            'size_label': row.size_label,
            'rating_label': row.rating.label,
            'qty': qty,
            'net_weight': float(weights.net_weight),
            'weight_source': weights.source,
            'weight_confidence': weights.confidence,
            'weight_rule': weights.rule_version,
            'lead_time_days': cost.lead_time_days,
            'dimension_row_id': row.pk,
            'standard': row.standard_id,
            'flange_type': row.flange_type_id,
            'rating': row.rating.code,
        },
        'costs': cost.rounded(),
        'price': price.as_dict(),
        'validation': val,
        'approval_required': bool(needs_approval),
        'snapshot': snapshot,
        'weight': weights.as_dict(),
    }
    return Calculation(
        output=output,
        row=row,
        material=material,
        material_price=library_material_price,
        weights=weights,
        cost=cost,
        price=price,
        validation=val,
        params=params,
        snapshot=snapshot,
        rate_versions=rate_versions,
    )


def calculate(payload: dict, *, user=None, on_date: Optional[date] = None) -> dict:
    """契约 §3.2：单条报价计算（不落库）。"""
    return _compute(payload, on_date=on_date, user=user).output


# ---------------------------------------------------------------- 编号
def next_quote_number(now=None) -> str:
    """生成 ``FQ-YYMM-####``；按月份统计已有最大流水号 +1。"""
    if now is None:
        now = timezone.localtime()
    elif isinstance(now, date) and not isinstance(now, datetime):
        now = datetime(now.year, now.month, now.day)
    elif timezone.is_aware(now):
        now = timezone.localtime(now)
    prefix = f'{QUOTE_NUMBER_PREFIX}-{now:%y%m}-'
    last = (
        Quotation.objects.filter(number__startswith=prefix)
        .aggregate(last=Max('number'))
        .get('last')
    )
    if last:
        match = re.search(r'(\d{4})$', last)
        seq = int(match.group(1)) + 1 if match else 1
    else:
        seq = 1
    if seq > QUOTE_NUMBER_SEQ_MAX:
        raise QuoteError(f'编号 {prefix} 当月流水号已用尽')
    return f'{prefix}{seq:04d}'


# ---------------------------------------------------------------- 创建 / 版本
def _quote_items_payload(payload: dict) -> tuple:
    """返回 ([(item_input, merged_params)], 顶层公共字段)。"""
    raw_items = payload.get('items')
    if raw_items is None:
        raw_items = [payload]
    if not isinstance(raw_items, list) or not raw_items:
        raise InvalidPayload('至少需要一条报价明细', field='items')

    shared = {
        key: payload[key]
        for key in ('surface', 'packaging', 'inspections', 'freight_per_kg',
                    'discount_pct', 'currency', 'fx_rate', 'design_temp_c',
                    'design_pressure_bar', 'yield', 'machining_factor',
                    'management_pct', 'profit_pct', 'tax_pct', 'on_date')
        if payload.get(key) is not None
    }
    shared_params = payload.get('params') if isinstance(payload.get('params'), dict) else {}
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise InvalidPayload('items 每项必须是对象', field='items')
        item_input = {**shared, **raw}
        if shared_params:
            item_input['params'] = {**shared_params, **(raw.get('params') or {})}
        items.append(item_input)
    return items, shared


def _resolve_customer(payload: dict):
    """解析 payload.customer（支持 code / id / {code|id}）；不存在则 400。"""
    customer_ref = payload.get('customer')
    if customer_ref in (None, ''):
        return None
    if isinstance(customer_ref, dict):
        customer_ref = customer_ref.get('code') or customer_ref.get('id')
    if customer_ref in (None, ''):
        return None
    customer = Customer.objects.filter(code=str(customer_ref)).first()
    if customer is None:
        raise InvalidPayload(f'客户不存在：{customer_ref}', field='customer')
    return customer


def _has_explicit_discount(items_payload: List[dict]) -> bool:
    """payload/items/params 中是否有显式 discount_pct（None/空视为未传）。"""
    for item in items_payload:
        if item.get('discount_pct') not in (None, ''):
            return True
        params = item.get('params')
        if isinstance(params, dict) and params.get('discount_pct') not in (None, ''):
            return True
    return False


def customer_default_discount(customer) -> Optional[Decimal]:
    """客户默认折扣，限制在 0–50%（价格计算上限）。客户为空返回 None。"""
    if customer is None or customer.discount_pct is None:
        return None
    discount = _dec(customer.discount_pct)
    if discount < 0:
        return Decimal('0')
    if discount > Decimal('50'):
        return Decimal('50')
    return discount


def _apply_customer_discount(items_payload: List[dict], customer) -> None:
    """payload 未显式传折扣时，把客户默认折扣注入每条明细（进入 _compute/快照/成本）。"""
    if customer is None or _has_explicit_discount(items_payload):
        return
    default_discount = customer_default_discount(customer)
    if default_discount is None:
        return
    for item in items_payload:
        item['discount_pct'] = default_discount
        params = item.get('params')
        if isinstance(params, dict) and params.get('discount_pct') in (None, ''):
            cleaned = dict(params)
            cleaned.pop('discount_pct', None)
            item['params'] = cleaned


def _collect_quote_totals(calcs: List[Calculation]) -> dict:
    subtotal = sum((c.price.amount_net for c in calcs), Decimal('0'))
    tax_amount = sum((c.price.tax_amount for c in calcs), Decimal('0'))
    return {
        'subtotal': costing.money(subtotal),
        'tax_amount': costing.money(tax_amount),
        'total': costing.money(subtotal + tax_amount),
    }


def _item_cost_params(calc: Calculation) -> dict:
    """明细成本参数：叠加快照里的演示标记，避免明细层静默标成真实费率。"""
    params = calc.cost.snapshot_params()
    snapshot_params = calc.snapshot.get('params') or {}
    for key in ('demo_rates', 'rate_source', 'demo_rate_codes',
                'default_rate_codes', 'real_rate_codes',
                'material_price_source', 'material_price_origin',
                'material_price_library_source', 'material_price_base',
                'demo_material_price',
                'machining_rate', 'machining_rate_source'):
        if key in snapshot_params:
            params[key] = snapshot_params[key]
    return params


def _create_quote_row(payload: dict, calcs: List[Calculation], user,
                      on_date: Optional[date] = None, customer=None) -> Quotation:
    first = calcs[0]
    params = first.params
    currency = params['currency']
    for calc in calcs[1:]:
        if calc.params['currency'] != currency:
            raise InvalidPayload('同一报价单的币种必须一致', field='currency')
    fx_rate = params['fx_rate']

    if customer is None:
        customer = _resolve_customer(payload)

    on_date = on_date or _parse_date(payload.get('on_date')) or timezone.localdate()
    valid_until = _parse_date(payload.get('valid_until')) or pricing.default_valid_until(on_date)
    validity = validation.validate_validity(valid_until, on_date)
    if validity['status'] == validation.STATUS_FAIL:
        raise ValidationFailed('；'.join(validity['messages']), field='valid_until')

    # 汇总快照（多明细合并）；演示标记按“任一条目涉及 demo”汇总
    merged_params = dict(first.snapshot['params'])
    merged_params['items'] = [c.snapshot['params'] for c in calcs]
    merged_params['quote_currency'] = currency
    merged_params['fx_rate'] = float(fx_rate)
    item_rate_sources = [c.snapshot['params'].get('rate_source', PROCESS_RATE) for c in calcs]
    merged_params['demo_rates'] = any(
        c.snapshot['params'].get('demo_rates') for c in calcs
    )
    if 'manual_override' in item_rate_sources:
        # 本次工资率手工覆盖必须保留，不能被 demo 汇总覆盖掉
        merged_params['rate_source'] = 'manual_override'
    elif not merged_params['demo_rates']:
        merged_params['rate_source'] = PROCESS_RATE
    elif set(item_rate_sources) == {DEMO_DEFAULTS}:
        merged_params['rate_source'] = DEMO_DEFAULTS
    else:
        merged_params['rate_source'] = DEMO_MIXED
    merged_params['demo_rate_codes'] = sorted(
        {code for c in calcs for code in (c.snapshot['params'].get('demo_rate_codes') or [])}
    )
    merged_params['demo_material_price'] = any(
        c.snapshot['params'].get('demo_material_price') for c in calcs
    )
    standard_editions: Dict[str, str] = {}
    price_versions: Dict[str, dict] = {}
    for calc in calcs:
        standard_editions.update(calc.snapshot['standard_editions'])
        for key, value in calc.snapshot['price_versions']['material_prices'].items():
            price_versions.setdefault(key, value)
    snapshot = CostSnapshot.objects.create(
        params=merged_params,
        standard_editions=standard_editions,
        price_versions={'material_prices': price_versions,
                        'process_rates': first.rate_versions},
        weight_rule=DEFAULT_WEIGHT_RULE,
        fx_rate=fx_rate,
        tax_rate=params['tax_pct'],
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )

    totals = _collect_quote_totals(calcs)
    incoterm = str(payload.get('incoterm') or 'EXW').upper()
    if incoterm not in dict(Quotation.INCOTERM_CHOICES):
        raise InvalidPayload(f'不支持的贸易条款：{incoterm}', field='incoterm')

    created_by = user if getattr(user, 'is_authenticated', False) else None
    for _attempt in range(5):
        number = next_quote_number()
        try:
            with transaction.atomic():
                quote = Quotation.objects.create(
                    number=number,
                    customer=customer,
                    status=Quotation.Status.DRAFT,
                    version=1,
                    currency=currency,
                    fx_rate=fx_rate,
                    incoterm=incoterm,
                    payment_terms=str(payload.get('payment_terms') or ''),
                    valid_until=valid_until,
                    subtotal=totals['subtotal'],
                    tax_amount=totals['tax_amount'],
                    total=totals['total'],
                    snapshot=snapshot,
                    created_by=created_by,
                    notes=str(payload.get('notes') or ''),
                )
            break
        except IntegrityError:
            continue
    else:
        raise QuoteError('报价编号生成冲突，请重试')

    for line_no, calc in enumerate(calcs, start=1):
        QuotationItem.objects.create(
            quotation=quote,
            line_no=line_no,
            dimension_row=calc.row,
            standard_edition=calc.row.standard.effective_edition,
            material=calc.material,
            material_price=Decimal(str(calc.snapshot['params']['material_price_per_kg'])),
            qty=calc.price.qty,
            net_weight=calc.weights.net_weight,
            weight_source=calc.weights.source,
            weight_confidence=calc.weights.confidence,
            weight_rule=calc.weights.rule_version,
            cost_breakdown={
                'costs': calc.cost.rounded(),
                'unit_cost': float(costing.money(calc.cost.total)),
                'unit_cost_raw': float(calc.cost.total),
                'params': _item_cost_params(calc),
                'price': calc.price.as_dict(),
            },
            unit_price=calc.price.unit_gross,
            amount=calc.price.amount_net,
            lead_time_days=calc.cost.lead_time_days,
            process={
                'surface': calc.cost.surface_option,
                'packaging': calc.cost.packaging_option,
                'inspections': list(calc.cost.inspections),
            },
            note=str(payload.get('note') or ''),
        )
    return quote


@transaction.atomic
def _create_quote_atomic(payload: dict, user, on_date: Optional[date] = None) -> Quotation:
    """单次创建事务；外层 create_quote 负责 SQLite 写锁重试。"""
    if not isinstance(payload, dict):
        raise InvalidPayload('请求体必须是 JSON 对象')
    items_payload, _shared = _quote_items_payload(payload)
    customer = _resolve_customer(payload)
    # 客户默认折扣：payload 未显式传 discount_pct 时生效，显式值优先，上限 50%
    _apply_customer_discount(items_payload, customer)
    calcs = [_compute(item, on_date=on_date, user=user) for item in items_payload]
    for calc in calcs:
        if calc.validation['status'] == validation.STATUS_FAIL:
            raise ValidationFailed(
                '；'.join(calc.validation['messages']) or '报价校验未通过',
                field='validation',
            )
    return _create_quote_row(payload, calcs, user, on_date=on_date, customer=customer)


def create_quote(payload: dict, user, on_date: Optional[date] = None) -> Quotation:
    """契约 §3.3：创建报价 → 编号 + V1 + 成本快照 + 明细。

    ``on_date`` 为报价日（可选）；显式实参 > payload["on_date"] > 本地今天。
    SQLite 只有一个写者：遇到 ``database is locked`` 时做有界退避重试。
    多人高并发录单仍建议 PostgreSQL（见部署指南），这里只保证小团队可用。
    """
    last_error = None
    for attempt in range(12):
        try:
            return _create_quote_atomic(payload, user, on_date=on_date)
        except OperationalError as exc:
            if 'database is locked' not in str(exc).lower() or attempt >= 11:
                raise
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    raise last_error  # pragma: no cover


def _quote_item_payloads(quote: Quotation) -> list:
    payloads = []
    for item in quote.items.select_related('dimension_row__standard',
                                           'dimension_row__rating',
                                           'dimension_row__flange_type',
                                           'material'):
        row = item.dimension_row
        process = item.process or {}
        payloads.append({
            'std': row.standard_id,
            'type': row.flange_type_id,
            'size': row.size,
            'rating': row.rating.code,
            'material': item.material.code,
            'qty': item.qty,
            'surface': process.get('surface'),
            'packaging': process.get('packaging'),
            'inspections': process.get('inspections') or [],
        })
    return payloads


def create_version(quote: Quotation, payload: Optional[dict], user, comment: str = '') -> Quotation:
    """修改已发送/已批准单 → 生成新版本（V+1），旧版归档（cancelled）。"""
    allowed = {
        Quotation.Status.DRAFT,
        Quotation.Status.PENDING,
        Quotation.Status.APPROVED,
        Quotation.Status.SENT,
        Quotation.Status.REJECTED,
    }
    if quote.status not in allowed:
        raise InvalidTransition(f'当前状态 {quote.status} 不能生成新版本')
    if payload is None:
        payload = {'items': _quote_item_payloads(quote)}
        snapshot_params = (quote.snapshot.params if quote.snapshot else {}) or {}
        simple = {k: snapshot_params.get(k) for k in (
            'yield', 'machining_factor', 'management_pct', 'profit_pct',
            'discount_pct', 'tax_pct', 'fx_rate', 'freight_per_kg') if snapshot_params.get(k) is not None}
        if simple:
            payload['params'] = simple
        if quote.customer_id:
            payload['customer'] = quote.customer.code
        payload['currency'] = quote.currency
        payload['fx_rate'] = quote.fx_rate
        payload['incoterm'] = quote.incoterm
        payload['payment_terms'] = quote.payment_terms
    new_quote = create_quote(payload, user)
    with transaction.atomic():
        new_quote.version = quote.version + 1
        new_quote.save(update_fields=['version', 'updated_at'])
        old_status = quote.status
        quote.status = Quotation.Status.CANCELLED
        quote.save(update_fields=['status', 'updated_at'])
        ApprovalLog.objects.create(
            quotation=quote,
            action=ApprovalLog.Action.CANCEL,
            actor=user if getattr(user, 'is_authenticated', False) else None,
            comment=comment or f'归档：{old_status} → 修订为 {new_quote.number} V{new_quote.version}',
        )
        ApprovalLog.objects.create(
            quotation=new_quote,
            action=ApprovalLog.Action.REVISE,
            actor=user if getattr(user, 'is_authenticated', False) else None,
            comment=comment or f'由 {quote.number} V{quote.version} 修订生成',
        )
    return new_quote


def copy_quote(quote: Quotation, user) -> Quotation:
    """rejected/expired 后复制为新报价（新编号、V1、草稿）。"""
    payload = {'items': _quote_item_payloads(quote)}
    snapshot_params = (quote.snapshot.params if quote.snapshot else {}) or {}
    simple = {k: snapshot_params.get(k) for k in (
        'yield', 'machining_factor', 'management_pct', 'profit_pct',
        'discount_pct', 'tax_pct', 'fx_rate', 'freight_per_kg') if snapshot_params.get(k) is not None}
    if simple:
        payload['params'] = simple
    if quote.customer_id:
        payload['customer'] = quote.customer.code
    payload['currency'] = quote.currency
    payload['fx_rate'] = quote.fx_rate
    payload['incoterm'] = quote.incoterm
    payload['payment_terms'] = quote.payment_terms
    return create_quote(payload, user)


# ---------------------------------------------------------------- 状态机
def _submit_target(quote: Quotation) -> str:
    return (
        Quotation.Status.PENDING if approval_required(quote) else Quotation.Status.APPROVED
    )


def _transition_map() -> dict:
    return {
        'submit': {
            Quotation.Status.DRAFT: _submit_target,
        },
        'approve': {
            Quotation.Status.PENDING: Quotation.Status.APPROVED,
        },
        'reject': {
            Quotation.Status.PENDING: Quotation.Status.DRAFT,
            Quotation.Status.SENT: Quotation.Status.REJECTED,
        },
        'send': {
            Quotation.Status.APPROVED: Quotation.Status.SENT,
        },
        'accept': {
            Quotation.Status.SENT: Quotation.Status.ACCEPTED,
        },
        'order': {
            Quotation.Status.ACCEPTED: Quotation.Status.ORDERED,
        },
        'expire': {
            Quotation.Status.SENT: Quotation.Status.EXPIRED,
            Quotation.Status.APPROVED: Quotation.Status.EXPIRED,
        },
    }


CANCELABLE_STATUSES = {
    Quotation.Status.DRAFT,
    Quotation.Status.PENDING,
    Quotation.Status.APPROVED,
    Quotation.Status.SENT,
    Quotation.Status.ACCEPTED,
    Quotation.Status.REJECTED,
    Quotation.Status.EXPIRED,
}

ACTION_ALIASES = {
    'pending_approval': 'submit',
    'pending': 'submit',
    'approve': 'approve',
    'send': 'send',
    'accept': 'accept',
    'order': 'order',
    'expire': 'expire',
    'cancel': 'cancel',
    'reject': 'reject',
    'submit': 'submit',
}


def quote_margin_pct(quote: Quotation) -> Decimal:
    """按明细成本快照计算整单毛利率（%）。"""
    amount = Decimal('0')
    cost_total = Decimal('0')
    for item in quote.items.all():
        amount += item.amount
        breakdown = item.cost_breakdown or {}
        unit_cost = _dec(breakdown.get('unit_cost_raw') or breakdown.get('unit_cost') or 0)
        cost_total += unit_cost * item.qty
    if amount <= 0:
        return Decimal('0')
    return ((amount - cost_total) / amount * Decimal('100')).quantize(
        Decimal('0.01')
    )


def approval_required(quote: Quotation) -> bool:
    """契约 §4 审批红线：折扣 > 5%、总额 > 50000、毛利率 < 8%。"""
    params = (quote.snapshot.params if quote.snapshot else {}) or {}
    discount = _dec(params.get('discount_pct') or 0)
    return pricing.approval_required(discount, quote.total, quote_margin_pct(quote))


def transition(quote: Quotation, action: str, user, comment: str = '') -> Quotation:
    """契约 §4 状态机；返回更新后的报价单。"""
    action_key = ACTION_ALIASES.get(str(action or '').strip().lower())
    if action_key is None:
        raise InvalidPayload(f'未知状态动作：{action}', field='action')
    current = quote.status

    if action_key == 'cancel':
        if current not in CANCELABLE_STATUSES:
            raise InvalidTransition(f'状态 {current} 不能作废')
        target = Quotation.Status.CANCELLED
    else:
        mapping = _transition_map().get(action_key)
        if mapping is None or current not in mapping:
            raise InvalidTransition(f'状态 {current} 不能执行 {action_key}')
        target = mapping[current]
        if callable(target):
            target = target(quote)

    actor = user if getattr(user, 'is_authenticated', False) else None
    with transaction.atomic():
        quote.status = target
        update_fields = ['status', 'updated_at']
        if action_key == 'approve':
            quote.approved_by = user
            update_fields.append('approved_by')
        quote.save(update_fields=update_fields)
        ApprovalLog.objects.create(
            quotation=quote,
            action=action_key,
            actor=actor,
            comment=comment or '',
        )
    return quote


# ---------------------------------------------------------------- 查询 / 序列化
def quotations_for_user(user):
    """按权限矩阵返回可见报价单 QuerySet。

    数据范围：sales 本人；engineering/finance/sales_manager/quote_admin 全部
    （见 ``quote.permissions.visible_quotations``）。未登录返回空集。
    """
    from .permissions import visible_quotations

    return visible_quotations(user)


def get_quote(number: str) -> Quotation:
    quote = (
        Quotation.objects.select_related('customer', 'snapshot', 'created_by', 'approved_by')
        .filter(number=str(number))
        .first()
    )
    if quote is None:
        raise QuoteNotFound(f'报价单不存在：{number}')
    return quote


def quote_to_dict(quote: Quotation, *, include_cost: bool = True) -> dict:
    """报价单 JSON；``include_cost=False`` 时剥离材料单价等成本字段（sales 用）。"""
    items = [
        {
            'line_no': item.line_no,
            'dimension_row_id': item.dimension_row_id,
            'description': item.dimension_row.title_zh if item.dimension_row_id else '',
            'standard_edition': item.standard_edition,
            'material': item.material.code,
            'material_price': float(item.material_price),
            'qty': item.qty,
            'net_weight': float(item.net_weight),
            'weight_source': item.weight_source,
            'weight_confidence': item.weight_confidence,
            'weight_rule': item.weight_rule,
            'unit_price': float(item.unit_price),
            'amount': float(item.amount),
            'lead_time_days': item.lead_time_days,
            'process': item.process,
        }
        for item in quote.items.select_related('dimension_row', 'material')
    ]
    if not include_cost:
        for item in items:
            item.pop('material_price', None)
    return {
        'id': quote.pk,
        'number': quote.number,
        'version': quote.version,
        'status': quote.status,
        'status_label': quote.get_status_display(),
        'customer': quote.customer.code if quote.customer_id else None,
        'currency': quote.currency,
        'fx_rate': float(quote.fx_rate),
        'incoterm': quote.incoterm,
        'payment_terms': quote.payment_terms,
        'valid_until': quote.valid_until.isoformat() if quote.valid_until else None,
        'subtotal': float(quote.subtotal),
        'tax_amount': float(quote.tax_amount),
        'total': float(quote.total),
        'margin_pct': float(quote_margin_pct(quote)),
        'approval_required': approval_required(quote),
        'notes': quote.notes,
        'created_by': quote.created_by.username if quote.created_by_id else None,
        'approved_by': quote.approved_by.username if quote.approved_by_id else None,
        'created_at': quote.created_at.isoformat() if quote.created_at else None,
        'updated_at': quote.updated_at.isoformat() if quote.updated_at else None,
        'items': items,
    }


# ---------------------------------------------------------------- 级联选项
def catalog_options() -> dict:
    """契约 §6 ``/quote/options/`` 的级联数据。"""
    tree: Dict[str, Dict[str, Dict[str, list]]] = {}
    rows = (
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating')
        .order_by('standard__sort_order', 'flange_type__sort_order',
                  'rating__sort_order', 'dn')
    )
    for row in rows:
        std = tree.setdefault(row.standard_id, {})
        type_bucket = std.setdefault(row.flange_type_id, {})
        size_list = type_bucket.setdefault(row.rating.code, [])
        size_list.append({
            'size': row.size,
            'size_label': row.size_label,
            'dn': row.dn,
        })

    standards = [
        {
            'id': std.id,
            'name': std.name,
            'short': std.short,
            'edition': std.edition,
            'dimension_edition': std.dimension_edition,
            'effective_edition': std.effective_edition,
            'ratings': [
                {'code': rating.code, 'label': rating.label}
                for rating in std.ratings.all()
            ],
            'types': sorted(tree.get(std.id, {}).keys()),
        }
        for std in Standard.objects.prefetch_related('ratings')
    ]
    types = [
        {'code': ft.slug, 'name': ft.name_zh, 'name_en': ft.name_en}
        for ft in FlangeType.objects.all()
    ]
    materials = [
        {
            'code': m.code,
            'label': m.label,
            'family': m.family,
            'density': float(m.density),
        }
        for m in Material.objects.filter(active=True)
    ]
    return {
        'standards': standards,
        'types': types,
        'materials': materials,
        'catalog': tree,
        'surfaces': [
            {'code': code, 'label': costing.SURFACE_LABELS[code],
             'rate': float(costing.SURFACE_RATES[code])}
            for code in costing.SURFACE_RATES
        ],
        'packaging': [
            {'code': code, 'label': costing.PACKAGING_LABELS[code],
             'fee': float(costing.PACKAGING_FEES[code])}
            for code in costing.PACKAGING_FEES
        ],
        'inspections': [
            {'code': code, 'label': costing.INSPECTION_LABELS[code],
             'fee': float(costing.INSPECTION_FEES[code])}
            for code in costing.INSPECTION_FEES
        ],
        'defaults': dict(pricing.DEFAULT_PARAMS),
        'weight_rule': DEFAULT_WEIGHT_RULE,
        'rate_table': costing.default_rate_table(),
    }
