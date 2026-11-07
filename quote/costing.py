"""成本计算（逐项可解释）。

与前端原型成本模型逐项对齐（``docs/法兰报价系统设计方案.html`` §成本）：

    毛坯重   = 净重 / 材料利用率
    材料费   = 毛坯重 × 材料单价
    加工费   = 加工档位(DN) × 类型系数 × 加工系数
    表面处理 = 表面费率(元/kg，按选项) × 净重
    检测费   = 固定项费用 + 第三方 3.5% × (材料费 + 加工费)
    包装费   = 包装选项固定费用
    运费     = 运费单价(元/kg) × 净重
    管理费   = 以上合计 × 管理费率
    单件成本 = 以上合计 + 管理费

金额全程 Decimal；对外输出时再四舍五入到 2 位（契约：金额 2 位、重量 3 位）。
本模块的费率常量是**演示默认值**（契约 §7），生产必须以 ProcessRate 价格库为准；
``QUOTE_DEMO_RATES`` 标记会写入成本快照，避免演示参数被静默用于生产。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Iterable, Mapping, Optional

QUOTE_DEMO_RATES = True  # 演示默认费率；ProcessRate 有数据时应覆盖

MONEY_Q = Decimal('0.01')
WEIGHT_Q = Decimal('0.001')

# ---- 演示默认费率（契约 §7；生产由财务提供真实值） ----
SURFACE_RATES = {
    'none': Decimal('0'),
    'rust': Decimal('1.2'),
    'zinc': Decimal('3.0'),
    'paint': Decimal('3.8'),
}
PACKAGING_FEES = {
    'none': Decimal('0'),
    'carton': Decimal('8'),
    'wood': Decimal('35'),
    'fumigated': Decimal('55'),
}
INSPECTION_FEES = {
    'pmi': Decimal('25'),
    'ut': Decimal('80'),
    'hydro': Decimal('40'),
    'hardness': Decimal('12'),
}
TYPE_FACTORS = {
    'weld-neck': Decimal('1.15'),
    'slip-on': Decimal('1.0'),
    'blind': Decimal('0.85'),
}
THIRD_PARTY_INSPECTION_PCT = Decimal('3.5')  # 占材料费+加工费
DEFAULT_YIELD = Decimal('0.70')
DEFAULT_MACHINING_FACTOR = Decimal('1.0')
DEFAULT_FREIGHT_PER_KG = Decimal('0.6')
DEFAULT_MANAGEMENT_PCT = Decimal('6')

SURFACE_LABELS = {
    'none': '不做处理',
    'rust': '防锈油',
    'zinc': '热镀锌',
    'paint': '喷涂',
}
PACKAGING_LABELS = {
    'none': '不包装',
    'carton': '纸箱',
    'wood': '木箱',
    'fumigated': '熏蒸木箱',
}
INSPECTION_LABELS = {
    'pmi': 'PMI 光谱',
    'ut': 'UT 探伤',
    'hydro': '水压试验',
    'hardness': '硬度检测',
    'third': '第三方检测',
}

MACHINING_TIERS = (
    (50, Decimal('12')),
    (100, Decimal('18')),
    (200, Decimal('28')),
    (350, Decimal('45')),
    (600, Decimal('75')),
    (1000, Decimal('120')),
)
MACHINING_TOP = Decimal('200')


class CostingError(ValueError):
    """成本参数非法。"""


def money(value) -> Decimal:
    value = value if isinstance(value, Decimal) else Decimal(str(value))
    return value.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def weight3(value) -> Decimal:
    value = value if isinstance(value, Decimal) else Decimal(str(value))
    return value.quantize(WEIGHT_Q, rounding=ROUND_HALF_UP)


def _dec(value, default=Decimal('0')) -> Decimal:
    if value is None:
        return default
    return value if isinstance(value, Decimal) else Decimal(str(value))


def machining_base(dn, tiers: Optional[Iterable] = None) -> Decimal:
    """按 DN 取加工费档位（元/件）。"""
    dn = int(dn)
    for limit, rate in (tiers or MACHINING_TIERS):
        if dn <= int(limit):
            return _dec(rate)
    return MACHINING_TOP


def type_factor(flange_type: str) -> Decimal:
    try:
        return TYPE_FACTORS[flange_type]
    except KeyError:
        raise CostingError(f'不支持的法兰类型：{flange_type}') from None


def surface_rate(option: str, rates: Optional[Mapping] = None) -> Decimal:
    options = SURFACE_RATES if rates is None else rates
    option = option or 'none'
    if option not in options:
        raise CostingError(f'未知表面处理选项：{option}')
    return _dec(options[option])


def packaging_fee(option: str, fees: Optional[Mapping] = None) -> Decimal:
    options = PACKAGING_FEES if fees is None else fees
    option = option or 'none'
    if option not in options:
        raise CostingError(f'未知包装选项：{option}')
    return _dec(options[option])


def inspection_fee(code: str, fees: Optional[Mapping] = None) -> Decimal:
    options = INSPECTION_FEES if fees is None else fees
    if code not in options:
        raise CostingError(f'未知检测选项：{code}')
    return _dec(options[code])


def normalize_inspections(inspections) -> tuple:
    if inspections is None:
        return ()
    if isinstance(inspections, str):
        inspections = [inspections]
    cleaned = []
    for item in inspections:
        code = str(item).strip()
        if not code:
            continue
        if code not in INSPECTION_LABELS:
            raise CostingError(f'未知检测选项：{code}')
        if code not in cleaned:
            cleaned.append(code)
    return tuple(cleaned)


def inspection_labels(inspections) -> list:
    return [INSPECTION_LABELS[c] for c in normalize_inspections(inspections)]


def lead_time_days(qty: int, surface: str = 'none', inspections=()) -> int:
    """交期：基础 7 天 + 数量阶梯 + 工艺附加。"""
    qty = max(1, int(qty))
    days = 7 + int(math.ceil(qty / 50.0))
    if (surface or 'none') != 'none':
        days += 2
    insp = set(normalize_inspections(inspections))
    if 'ut' in insp:
        days += 3
    if 'third' in insp:
        days += 5
    return days


@dataclass
class CostResult:
    """单件成本明细；金额字段保留 Decimal 原始值，输出时再取 2 位。"""

    net_weight: Decimal
    blank_weight: Decimal
    material: Decimal
    machining: Decimal
    surface_cost: Decimal
    inspection: Decimal
    packaging: Decimal
    freight: Decimal
    base: Decimal
    management: Decimal
    total: Decimal
    machining_base: Decimal
    type_factor_value: Decimal
    machining_factor: Decimal
    surface_rate_value: Decimal
    freight_rate: Decimal
    management_pct: Decimal
    inspection_fixed: Decimal
    third_party: Decimal
    yield_pct: Decimal
    surface_option: str = 'none'
    packaging_option: str = 'none'
    inspections: tuple = field(default_factory=tuple)
    lead_time_days: int = 0
    demo_rates: bool = QUOTE_DEMO_RATES

    @property
    def unit_cost(self) -> Decimal:
        return self.total

    def rounded(self) -> Dict[str, float]:
        """契约 §3.2 ``costs`` 输出（2 位小数）。"""
        return {
            'material': float(money(self.material)),
            'machining': float(money(self.machining)),
            'surface': float(money(self.surface_cost)),
            'inspection': float(money(self.inspection)),
            'packaging': float(money(self.packaging)),
            'freight': float(money(self.freight)),
            'management': float(money(self.management)),
            'total': float(money(self.total)),
        }

    def as_dict(self) -> Dict[str, object]:
        data = self.rounded()
        data.update({
            'blank_weight': float(weight3(self.blank_weight)),
            'net_weight': float(weight3(self.net_weight)),
            'machining_base': float(self.machining_base),
            'type_factor': float(self.type_factor_value),
            'machining_factor': float(self.machining_factor),
            'surface_rate': float(self.surface_rate_value),
            'freight_rate': float(self.freight_rate),
            'management_pct': float(self.management_pct),
            'inspection_fixed': float(money(self.inspection_fixed)),
            'third_party': float(money(self.third_party)),
            'yield_pct': float(self.yield_pct),
            'surface_option': self.surface_option,
            'packaging_option': self.packaging_option,
            'inspections': list(self.inspections),
            'lead_time_days': self.lead_time_days,
            'demo_rates': self.demo_rates,
        })
        return data

    def snapshot_params(self) -> Dict[str, object]:
        return {
            'net_weight': float(weight3(self.net_weight)),
            'blank_weight': float(weight3(self.blank_weight)),
            'machining_base': float(self.machining_base),
            'type_factor': float(self.type_factor_value),
            'machining_factor': float(self.machining_factor),
            'surface_rate': float(self.surface_rate_value),
            'freight_rate': float(self.freight_rate),
            'management_pct': float(self.management_pct),
            'inspection_fixed': float(money(self.inspection_fixed)),
            'third_party_pct': float(THIRD_PARTY_INSPECTION_PCT),
            'third_party': float(money(self.third_party)),
            'yield_pct': float(self.yield_pct),
            'surface_option': self.surface_option,
            'packaging_option': self.packaging_option,
            'inspections': list(self.inspections),
            'lead_time_days': self.lead_time_days,
            'demo_rates': self.demo_rates,
            'rate_source': 'demo_defaults' if self.demo_rates else 'process_rate',
        }


def compute_cost(
    *,
    net_weight,
    dn: int,
    flange_type: str,
    qty: int,
    material_price,
    yield_pct=DEFAULT_YIELD,
    machining_factor=DEFAULT_MACHINING_FACTOR,
    surface: str = 'none',
    packaging: str = 'wood',
    inspections=(),
    freight_per_kg=DEFAULT_FREIGHT_PER_KG,
    management_pct=DEFAULT_MANAGEMENT_PCT,
    machining_base_override=None,
    machining_tiers=None,
    surface_rates: Optional[Mapping] = None,
    packaging_fees: Optional[Mapping] = None,
    inspection_fees: Optional[Mapping] = None,
    demo_rates: bool = QUOTE_DEMO_RATES,
) -> CostResult:
    """计算单件成本；所有中间值保留 Decimal，最后由调用方取 2 位。"""
    net = _dec(net_weight)
    if net <= 0:
        raise CostingError('净重必须大于 0')
    qty = int(qty)
    if qty < 1:
        raise CostingError('数量必须 ≥ 1')
    yield_pct = _dec(yield_pct)
    if not Decimal('0') < yield_pct <= Decimal('1'):
        raise CostingError('材料利用率必须在 (0, 1] 区间')
    material_price = _dec(material_price)
    if material_price <= 0:
        raise CostingError('材料单价必须大于 0')
    machining_factor = _dec(machining_factor)
    if machining_factor <= 0:
        raise CostingError('加工系数必须大于 0')
    management_pct = _dec(management_pct)
    if management_pct < 0:
        raise CostingError('管理费率不能为负')
    freight_per_kg = _dec(freight_per_kg)
    if freight_per_kg < 0:
        raise CostingError('运费单价不能为负')

    inspections = normalize_inspections(inspections)
    mach_base = (
        _dec(machining_base_override)
        if machining_base_override is not None
        else machining_base(dn, machining_tiers)
    )
    factor = type_factor(flange_type)
    s_rate = surface_rate(surface, surface_rates)
    pack_fee = packaging_fee(packaging, packaging_fees)

    blank = net / yield_pct
    material = blank * material_price
    machining = mach_base * factor * machining_factor
    surface_cost = s_rate * net
    insp_fixed = sum(
        (inspection_fee(code, inspection_fees) for code in inspections if code != 'third'),
        Decimal('0'),
    )
    third = (
        THIRD_PARTY_INSPECTION_PCT / Decimal('100') * (material + machining)
        if 'third' in inspections
        else Decimal('0')
    )
    inspection = insp_fixed + third
    freight = freight_per_kg * net

    base = material + machining + surface_cost + inspection + pack_fee + freight
    management = base * management_pct / Decimal('100')
    total = base + management

    return CostResult(
        net_weight=net,
        blank_weight=blank,
        material=material,
        machining=machining,
        surface_cost=surface_cost,
        inspection=inspection,
        packaging=pack_fee,
        freight=freight,
        base=base,
        management=management,
        total=total,
        machining_base=mach_base,
        type_factor_value=factor,
        machining_factor=machining_factor,
        surface_rate_value=s_rate,
        freight_rate=freight_per_kg,
        management_pct=management_pct,
        inspection_fixed=insp_fixed,
        third_party=third,
        yield_pct=yield_pct,
        surface_option=surface or 'none',
        packaging_option=packaging or 'none',
        inspections=inspections,
        lead_time_days=lead_time_days(qty, surface, inspections),
        demo_rates=demo_rates,
    )


def default_rate_table() -> Dict[str, object]:
    """演示费率表（供 options/快照展示，生产以价格库为准）。"""
    return {
        'surface_rates': {k: float(v) for k, v in SURFACE_RATES.items()},
        'packaging_fees': {k: float(v) for k, v in PACKAGING_FEES.items()},
        'inspection_fees': {k: float(v) for k, v in INSPECTION_FEES.items()},
        'type_factors': {k: float(v) for k, v in TYPE_FACTORS.items()},
        'machining_tiers': [[int(limit), float(rate)] for limit, rate in MACHINING_TIERS],
        'machining_top': float(MACHINING_TOP),
        'third_party_pct': float(THIRD_PARTY_INSPECTION_PCT),
        'demo_rates': QUOTE_DEMO_RATES,
    }
