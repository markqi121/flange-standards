"""定价：成本加成 → 客户折扣 → 数量阶梯 → 税率；以及材料价/费率取值。

规则（与前端原型一致，契约 §3.4/§4/§7）：

    单件净价 = 单件成本 × (1 + 利润率) × (1 − 客户折扣) × (1 − 数量折扣)
    单件税额 = 单件净价 × 税率
    单件含税 = 单件净价 + 单件税额
    不含税金额 = round(单件净价 × 数量)
    税额       = round(单件税额 × 数量)
    含税总额   = 不含税金额 + 税额
    毛利率     = (不含税金额 − 单件成本 × 数量) / 不含税金额

- 金额全程 Decimal，最后四舍五入 2 位；毛利率保留 1 位（契约示例 8.9）；
- 审批红线：折扣 > 5%、含税总额 > 50000、毛利率 < 8%；
- 价格一律先按 CNY 计算；USD 报价由 ``fx_rate`` 在展示层换算（与前端一致）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.utils import timezone

MONEY_Q = Decimal('0.01')
PCT_Q = Decimal('0.01')
MARGIN_Q = Decimal('0.1')

# 契约 §7 演示默认参数（生产必须替换为财务真实参数）
DEFAULT_PARAMS = {
    'yield': '0.70',
    'machining_factor': '1.0',
    'management_pct': '6',
    'profit_pct': '12',
    'discount_pct': '0',
    'tax_pct': '13',
    'fx_rate': '7.10',
    'currency': 'CNY',
}

# 数量阶梯折扣（%，与前端 qtyDiscount 一致）
QTY_DISCOUNT_TIERS = (
    (200, Decimal('6')),
    (50, Decimal('4')),
    (10, Decimal('2')),
)

APPROVAL_DISCOUNT_LIMIT_PCT = Decimal('5')
APPROVAL_TOTAL_LIMIT = Decimal('50000')
APPROVAL_MARGIN_FLOOR_PCT = Decimal('8')

QUOTE_VALID_DAYS = 15


class PriceError(ValueError):
    """价格/费率不可用或参数非法。"""


def money(value) -> Decimal:
    value = value if isinstance(value, Decimal) else Decimal(str(value))
    return value.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def pct2(value) -> Decimal:
    value = value if isinstance(value, Decimal) else Decimal(str(value))
    return value.quantize(PCT_Q, rounding=ROUND_HALF_UP)


def _dec(value, default=Decimal('0')) -> Decimal:
    if value is None:
        return default
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ---------------------------------------------------------------- 价格库取值
def price_for(material, on_date: Optional[date] = None):
    """取报价日 <= 生效日期的最近一条材料价（契约 §2.2）。

    无可用价格时抛 ``PriceError``（不能静默用 0/演示价）。
    """
    from .models import MaterialPrice

    on_date = on_date or timezone.localdate()
    price = (
        MaterialPrice.objects.filter(material=material, effective_from__lte=on_date)
        .order_by('-effective_from', '-id')
        .first()
    )
    if price is None:
        raise PriceError(
            f'材料 {material.code} 在 {on_date} 没有生效价格，请先在价格库维护'
        )
    if price.currency != 'CNY':
        raise PriceError(f'材料价币种 {price.currency} 暂不支持，报价基准为 CNY')
    if price.price_per_kg <= 0:
        raise PriceError(f'材料 {material.code} 的价格必须大于 0')
    return price


def _match_tier(key, dn) -> bool:
    key = str(key).strip().lower().replace('dn', '').replace('mm', '').strip()
    if key.startswith('<='):
        return dn <= int(float(key[2:]))
    if key.startswith('<'):
        return dn < int(float(key[1:]))
    if key.startswith('>='):
        return dn >= int(float(key[2:]))
    if key.startswith('>'):
        return dn > int(float(key[1:]))
    try:
        return dn == int(float(key))
    except ValueError:
        return False


def rate_for(code: str, on_date: Optional[date] = None, dn: Optional[int] = None) -> Decimal:
    """取工艺费率；``tiers`` 按 DN 匹配，未命中用基础费率（契约 §3.4）。"""
    from .models import ProcessRate

    on_date = on_date or timezone.localdate()
    rate = (
        ProcessRate.objects.filter(code=code, effective_from__lte=on_date)
        .order_by('-effective_from', '-id')
        .first()
    )
    if rate is None:
        raise PriceError(f'费率 {code} 在 {on_date} 没有生效记录')
    if dn is not None and isinstance(rate.tiers, dict) and rate.tiers:
        for key, value in rate.tiers.items():
            if _match_tier(key, dn):
                return _dec(value)
    return _dec(rate.rate)


def all_rates(on_date: Optional[date] = None) -> dict:
    """返回全部生效费率（{code: ProcessRate}），供成本/快照使用。"""
    from .models import ProcessRate

    on_date = on_date or timezone.localdate()
    result = {}
    for rate in ProcessRate.objects.filter(effective_from__lte=on_date).order_by('code', '-effective_from', '-id'):
        result.setdefault(rate.code, rate)
    return result


# ---------------------------------------------------------------- 折扣 / 有效期
def quantity_discount_pct(qty) -> Decimal:
    qty = int(qty or 0)
    for threshold, discount in QTY_DISCOUNT_TIERS:
        if qty >= threshold:
            return discount
    return Decimal('0')


def combine_discount(customer_discount_pct, extra_discount_pct=Decimal('0')) -> Decimal:
    """客户默认折扣 + 本次折扣（简单相加），限定 0–50%。"""
    total = _dec(customer_discount_pct) + _dec(extra_discount_pct)
    if total < 0:
        return Decimal('0')
    if total > Decimal('50'):
        return Decimal('50')
    return pct2(total)


def default_valid_until(on_date: Optional[date] = None) -> date:
    on_date = on_date or timezone.localdate()
    return on_date + timedelta(days=QUOTE_VALID_DAYS)


# ---------------------------------------------------------------- 定价
def margin_pct(amount_net, unit_cost, qty) -> Decimal:
    amount_net = _dec(amount_net)
    if amount_net <= 0:
        return Decimal('0')
    gross_margin = amount_net - _dec(unit_cost) * int(qty)
    return (gross_margin / amount_net * Decimal('100')).quantize(
        MARGIN_Q, rounding=ROUND_HALF_UP
    )


def approval_required(discount_pct, total_gross, margin) -> bool:
    """审批红线：折扣 > 5%、含税总额 > 50000、毛利率 < 8%（契约 §4）。"""
    return bool(
        _dec(discount_pct) > APPROVAL_DISCOUNT_LIMIT_PCT
        or _dec(total_gross) > APPROVAL_TOTAL_LIMIT
        or _dec(margin) < APPROVAL_MARGIN_FLOOR_PCT
    )


@dataclass
class PriceResult:
    unit_net: Decimal
    unit_tax: Decimal
    unit_gross: Decimal
    amount_net: Decimal
    tax_amount: Decimal
    total_gross: Decimal
    margin_pct: Decimal
    margin_raw: Decimal
    per_kg: Decimal
    quantity_discount_pct: Decimal
    discount_pct: Decimal
    profit_pct: Decimal
    tax_pct: Decimal
    qty: int
    currency: str = 'CNY'
    fx_rate: Decimal = Decimal('7.1000')

    def as_dict(self) -> dict:
        return {
            'unit_net': float(money(self.unit_net)),
            'unit_tax': float(money(self.unit_tax)),
            'unit_gross': float(money(self.unit_gross)),
            'amount_net': float(money(self.amount_net)),
            'tax_amount': float(money(self.tax_amount)),
            'total_gross': float(money(self.total_gross)),
            'margin_pct': float(self.margin_pct),
            'per_kg': float(money(self.per_kg)),
            'quantity_discount_pct': float(pct2(self.quantity_discount_pct)),
            'discount_pct': float(pct2(self.discount_pct)),
            'profit_pct': float(pct2(self.profit_pct)),
            'tax_pct': float(pct2(self.tax_pct)),
            'currency': self.currency,
            'fx_rate': float(self.fx_rate),
        }


def compute_price(
    *,
    unit_cost,
    qty: int,
    profit_pct=DEFAULT_PARAMS['profit_pct'],
    discount_pct=DEFAULT_PARAMS['discount_pct'],
    tax_pct=DEFAULT_PARAMS['tax_pct'],
    net_weight=Decimal('0'),
    currency: str = 'CNY',
    fx_rate=DEFAULT_PARAMS['fx_rate'],
) -> PriceResult:
    """按成本加成法算单件/整单价格（契约 §3.2）。"""
    unit_cost = _dec(unit_cost)
    if unit_cost < 0:
        raise PriceError('单件成本不能为负')
    qty = int(qty)
    if qty < 1:
        raise PriceError('数量必须 ≥ 1')
    profit_pct = _dec(profit_pct)
    discount_pct = _dec(discount_pct)
    tax_pct = _dec(tax_pct)
    fx_rate = _dec(fx_rate, Decimal('7.10'))
    if discount_pct < 0 or discount_pct > 50:
        raise PriceError('折扣必须在 0–50% 之间')
    if tax_pct < 0:
        raise PriceError('税率不能为负')
    if currency not in {'CNY', 'USD'}:
        raise PriceError(f'不支持的币种：{currency}')
    if currency == 'USD' and fx_rate <= 0:
        raise PriceError('USD 报价的汇率必须大于 0')

    qd = quantity_discount_pct(qty)

    # 前端原型使用浮点乘法后 round2；这里用 Decimal 同顺序计算，结果一致
    net_cny = (
        unit_cost
        * (Decimal('1') + profit_pct / Decimal('100'))
        * (Decimal('1') - discount_pct / Decimal('100'))
        * (Decimal('1') - qd / Decimal('100'))
    )
    unit_net = money(net_cny)
    unit_tax = money(unit_net * tax_pct / Decimal('100'))
    unit_gross = money(unit_net + unit_tax)
    amount_net = money(unit_net * qty)
    tax_amount = money(unit_tax * qty)
    total_gross = money(amount_net + tax_amount)
    margin_raw = (
        (amount_net - unit_cost * qty) / amount_net * Decimal('100')
        if amount_net > 0
        else Decimal('0')
    )
    margin = margin_raw.quantize(MARGIN_Q, rounding=ROUND_HALF_UP)
    per_kg = money(unit_gross / _dec(net_weight)) if _dec(net_weight) > 0 else Decimal('0')

    return PriceResult(
        unit_net=unit_net,
        unit_tax=unit_tax,
        unit_gross=unit_gross,
        amount_net=amount_net,
        tax_amount=tax_amount,
        total_gross=total_gross,
        margin_pct=margin,
        margin_raw=margin_raw,
        per_kg=per_kg,
        quantity_discount_pct=qd,
        discount_pct=pct2(discount_pct),
        profit_pct=pct2(profit_pct),
        tax_pct=pct2(tax_pct),
        qty=qty,
        currency=currency,
        fx_rate=fx_rate,
    )
