"""价格库数据层：按报价日取价、CSV 导入解析/校验、客户折扣解析。

职责边界（见 ``docs/报价功能-接口契约.md`` §2.2–2.4、§3.4、§5）：

- **取价**：报价日 ``<=`` 生效日期的最近一条材料价 / 费率；无生效记录必须报错，
  不能静默用 0 或演示价。核心实现由 backend-core 在 ``quote/pricing.py`` 完成
  （``price_for`` / ``rate_for`` / ``all_rates``），本模块只做数据层 facade 转发，
  不重复实现 costing / pricing 规则。
- **CSV 导入**：把材料价格 / 工艺费率 / 客户的 CSV 解析成带类型的行，逐行校验并
  返回行号级错误；实际写库由 ``import_prices`` 管理命令调用 ``import_csv`` 完成。
  唯一键（材料+生效日期 / 费率代码 / 客户编码）使用 ``update_or_create``，重复导入
  幂等。
- **客户折扣**：客户默认折扣（契约 0–30%）与本次额外折扣合并；合并上限 50%，
  最终价格由 ``pricing.compute_price`` 应用。
- **演示数据**：``MaterialPrice.source`` / ``ProcessRate.source`` 均支持
  ``'demo'``（T5 已加入 ``SOURCE_CHOICES`` 并生成迁移），演示价统一写
  ``source='demo'``；不得把演示价伪装成 purchase/market/manual。

CSV 模板列（也可用 ``manage.py import_prices --kind <kind> --template -`` 打印）：

``materials``: ``material_code,price_per_kg,currency,effective_from,source,note``
``rates``:     ``code,label,unit,rate,tiers,effective_from``
``customers``: ``code,name,level,discount_pct,currency,payment_terms,active``
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional, Sequence

from django.db import transaction

from . import pricing
from .models import Customer, Material, MaterialPrice, ProcessRate

# --------------------------------------------------------------------------- 常量
DEMO_SOURCE = 'demo'
DEMO_SOURCE_LABEL = '示例（演示）'
DEMO_SOURCE_CHOICE = (DEMO_SOURCE, DEMO_SOURCE_LABEL)

SOURCE_LABELS = {**dict(MaterialPrice.SOURCE_CHOICES), DEMO_SOURCE: DEMO_SOURCE_LABEL}
SOURCE_CODES = tuple(SOURCE_LABELS)

CURRENCIES = ('CNY', 'USD')
# 契约 §2.2：材料单价人民币元/kg；报价基准币种为 CNY。
MATERIAL_CURRENCIES = ('CNY',)
CUSTOMER_LEVELS = ('A', 'B', 'C')
CUSTOMER_DISCOUNT_MIN = Decimal('0')
CUSTOMER_DISCOUNT_MAX = Decimal('30')

# 取价失败异常（与 pricing 共用，调用方只需捕获一个类型）
PriceError = pricing.PriceError

MATERIAL_PRICE_COLUMNS = (
    'material_code',
    'price_per_kg',
    'currency',
    'effective_from',
    'source',
    'note',
)
PROCESS_RATE_COLUMNS = ('code', 'label', 'unit', 'rate', 'tiers', 'effective_from')
CUSTOMER_COLUMNS = (
    'code',
    'name',
    'level',
    'discount_pct',
    'currency',
    'payment_terms',
    'active',
)

# kind -> (中文名, 列, 必需列, 示例行)
CSV_KINDS = {
    'materials': {
        'label': '材料价格',
        'columns': MATERIAL_PRICE_COLUMNS,
        'required': ('material_code', 'price_per_kg', 'effective_from'),
        'example': (
            'A105',
            '7.20',
            'CNY',
            '2026-01-01',
            DEMO_SOURCE,
            '演示价（示例），禁止用于生产报价',
        ),
    },
    'rates': {
        'label': '工艺费率',
        'columns': PROCESS_RATE_COLUMNS,
        'required': ('code', 'unit', 'rate', 'effective_from'),
        'example': (
            'machining',
            '加工费',
            'per_piece',
            '200',
            '{"50": 12, "100": 18, "200": 28, "350": 45, "600": 75, "1000": 120}',
            '2026-01-01',
        ),
    },
    'customers': {
        'label': '客户',
        'columns': CUSTOMER_COLUMNS,
        'required': ('code', 'name', 'level'),
        'example': (
            'demo-a',
            '示例客户 A（演示）',
            'A',
            '8.00',
            'CNY',
            '30% 预付，余款发货前',
            'true',
        ),
    },
}

# 表头别名：规范化为小写/下划线后查表，支持中文导出表头。
HEADER_ALIASES = {
    'materials': {
        'material': 'material_code',
        'material_code': 'material_code',
        'code': 'material_code',
        '牌号': 'material_code',
        '牌号代码': 'material_code',
        '材质': 'material_code',
        '材质代码': 'material_code',
        'price': 'price_per_kg',
        'price_per_kg': 'price_per_kg',
        'unit_price': 'price_per_kg',
        '单价': 'price_per_kg',
        '材料单价': 'price_per_kg',
        '元/kg': 'price_per_kg',
        'currency': 'currency',
        '币种': 'currency',
        'effective_from': 'effective_from',
        'effective_date': 'effective_from',
        '生效日期': 'effective_from',
        'source': 'source',
        '来源': 'source',
        'note': 'note',
        'remark': 'note',
        '备注': 'note',
    },
    'rates': {
        'code': 'code',
        'rate_code': 'code',
        '费率代码': 'code',
        '编码': 'code',
        'label': 'label',
        'name': 'label',
        '展示名': 'label',
        '名称': 'label',
        'unit': 'unit',
        '单位': 'unit',
        'rate': 'rate',
        '费率': 'rate',
        '基础费率': 'rate',
        'tiers': 'tiers',
        '档位': 'tiers',
        'effective_from': 'effective_from',
        'effective_date': 'effective_from',
        '生效日期': 'effective_from',
    },
    'customers': {
        'code': 'code',
        'customer_code': 'code',
        '客户编码': 'code',
        '编码': 'code',
        'name': 'name',
        'customer_name': 'name',
        '客户名称': 'name',
        '名称': 'name',
        'level': 'level',
        '等级': 'level',
        'discount_pct': 'discount_pct',
        'discount': 'discount_pct',
        '折扣': 'discount_pct',
        '默认折扣': 'discount_pct',
        'currency': 'currency',
        '币种': 'currency',
        'payment_terms': 'payment_terms',
        '账期': 'payment_terms',
        'active': 'active',
        '启用': 'active',
    },
}

UNIT_ALIASES = {
    'per_piece': 'per_piece',
    'perpiece': 'per_piece',
    'piece': 'per_piece',
    '元/件': 'per_piece',
    '件': 'per_piece',
    'per_kg': 'per_kg',
    'perkg': 'per_kg',
    'kg': 'per_kg',
    '元/kg': 'per_kg',
    '元/千克': 'per_kg',
    'percent': 'percent',
    'pct': 'percent',
    '%': 'percent',
    '百分比': 'percent',
    'per_hour': 'per_hour',
    'perhour': 'per_hour',
    'hour': 'per_hour',
    '元/时': 'per_hour',
    '元/小时': 'per_hour',
}

SOURCE_ALIASES = {
    'purchase': 'purchase',
    '采购': 'purchase',
    'market': 'market',
    '市场': 'market',
    'manual': 'manual',
    '手工': 'manual',
    'demo': DEMO_SOURCE,
    '示例': DEMO_SOURCE,
    '演示': DEMO_SOURCE,
}

DATE_FORMATS = ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y%m%d')
_SLUG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


# --------------------------------------------------------------------------- 异常
class CSVError(ValueError):
    """CSV 文件级错误（空文件、缺表头、类型未知等）。"""


class _RowError(Exception):
    """单行校验错误，由 :func:`parse_rows` 转成带行号的 :class:`CSVRowError`。"""

    def __init__(self, message: str, field: str = ''):
        super().__init__(message)
        self.message = message
        self.field = field


@dataclass(frozen=True)
class CSVRowError:
    """一行 CSV 的校验错误（含 CSV 行号，供导入命令逐行报告）。"""

    line: int
    message: str
    field: str = ''

    def __str__(self) -> str:
        if self.field:
            return f'第 {self.line} 行 [{self.field}]：{self.message}'
        return f'第 {self.line} 行：{self.message}'


@dataclass
class ImportResult:
    """一次 CSV 导入的结果汇总。"""

    kind: str
    row_count: int = 0
    created: int = 0
    updated: int = 0
    errors: list = field(default_factory=list)
    applied: bool = False

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------- 取价
def price_for(material, on_date: Optional[date] = None) -> MaterialPrice:
    """取报价日 ``<=`` 生效日期的最近一条材料价。

    :raises pricing.PriceError: 无生效价格 / 币种不是 CNY / 价格非正。
    """
    return pricing.price_for(material, on_date)


def rate_for(code: str, on_date: Optional[date] = None, dn: Optional[int] = None) -> Decimal:
    """取工艺费率；``tiers`` 按 DN 匹配，未命中用基础费率。"""
    return pricing.rate_for(code, on_date, dn)


def all_rates(on_date: Optional[date] = None) -> dict:
    """全部生效费率 ``{code: ProcessRate}``（每个 code 取最近生效一条）。"""
    return pricing.all_rates(on_date)


def material_prices_on(on_date: Optional[date] = None) -> dict:
    """全部材质在给定日期的生效材料价 ``{material_code: MaterialPrice}``。

    没有生效价格的材质不会出现在结果里（调用方可用 :func:`price_for` 得到明确错误）。
    """
    on_date = on_date or _today()
    result: dict = {}
    for material in Material.objects.filter(active=True).order_by('sort_order', 'code'):
        try:
            result[material.code] = pricing.price_for(material, on_date)
        except pricing.PriceError:
            continue
    return result


def _today() -> date:
    from django.utils import timezone

    return timezone.localdate()


# --------------------------------------------------------------------------- 客户折扣
def customer_discount_pct(customer: Optional[Customer]) -> Decimal:
    """客户默认折扣（%，0–30）；无客户返回 0。"""
    if customer is None:
        return Decimal('0')
    value = _decimal_or_zero(getattr(customer, 'discount_pct', None))
    if value < CUSTOMER_DISCOUNT_MIN or value > CUSTOMER_DISCOUNT_MAX:
        raise pricing.PriceError(
            f'客户 {getattr(customer, "code", customer)} 的默认折扣 {value}% 超出 0–30%'
        )
    return value


def resolve_discount(customer: Optional[Customer], extra_pct=Decimal('0')) -> Decimal:
    """客户默认折扣 + 本次额外折扣（简单相加，限定 0–50%）。"""
    extra = _decimal_or_zero(extra_pct)
    if extra < 0:
        raise pricing.PriceError('额外折扣不能为负')
    return pricing.combine_discount(customer_discount_pct(customer), extra)


def discount_source(customer: Optional[Customer], extra_pct=Decimal('0')) -> dict:
    """折扣明细（便于写快照 / 前端展示），金额仍由 pricing 计算。"""
    base = customer_discount_pct(customer)
    extra = _decimal_or_zero(extra_pct)
    return {
        'customer_code': getattr(customer, 'code', '') if customer is not None else '',
        'customer_discount_pct': pricing.pct2(base),
        'extra_discount_pct': pricing.pct2(extra),
        'total_discount_pct': resolve_discount(customer, extra),
    }


def _decimal_or_zero(value) -> Decimal:
    if value is None or value == '':
        return Decimal('0')
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise pricing.PriceError(f'折扣值非法：{value!r}') from None


# --------------------------------------------------------------------------- CSV 读取
def read_csv(source, encoding: str = 'utf-8-sig') -> list:
    """读取 CSV，返回 ``[(行号, {规范化表头: 去空白值}), ...]``。

    ``source`` 可以是文件路径、文本流（含 ``io.StringIO`` / ``sys.stdin``）。
    空行自动跳过；行号取 CSV 物理行号（从 2 开始，1 是表头）。
    """
    if hasattr(source, 'read'):
        text = source.read()
        if isinstance(text, bytes):
            text = text.decode(encoding)
    else:
        path = Path(source)
        if not path.exists():
            raise CSVError(f'CSV 文件不存在：{path}')
        try:
            text = path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            raise CSVError(f'CSV 编码不是 {encoding}：{exc}') from exc

    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames
    except csv.Error as exc:
        raise CSVError(f'CSV 解析失败：{exc}') from exc
    if not fieldnames:
        raise CSVError('CSV 没有表头')

    rows = []
    try:
        for raw in reader:
            if not raw:
                continue
            values = {}
            blank = True
            for key, value in raw.items():
                if key is None:
                    # 多余列（行内字段数 > 表头）不参与导入
                    continue
                text_value = value.strip() if isinstance(value, str) else value
                values[_normalize_header(key)] = text_value
                if text_value not in (None, ''):
                    blank = False
            if blank:
                continue
            rows.append((reader.line_num, values))
    except csv.Error as exc:
        raise CSVError(f'CSV 解析失败：{exc}') from exc
    return rows


def _normalize_header(name) -> str:
    text = str(name or '').strip().lstrip('\ufeff').replace('\u3000', ' ')
    text = text.lower()
    text = re.sub(r'[\s\-]+', '_', text).strip('_')
    return text


def _canonical_rows(kind: str, rows: Iterable) -> list:
    """把表头别名映射成规范列名，并校验必需列存在。"""
    if kind not in CSV_KINDS:
        raise CSVError(f'未知的导入类型：{kind}（可选：{", ".join(sorted(CSV_KINDS))}）')
    aliases = HEADER_ALIASES[kind]
    canonical = []
    headers = set()
    for line, raw in rows:
        mapped = {}
        for key, value in raw.items():
            mapped[aliases.get(key, key)] = value
        headers.update(mapped)
        canonical.append((line, mapped))

    required = CSV_KINDS[kind]['required']
    missing = [name for name in required if name not in headers]
    if missing:
        raise CSVError(
            f'CSV 缺少必需列：{", ".join(missing)}；'
            f'模板列：{", ".join(CSV_KINDS[kind]["columns"])}'
        )
    return canonical


# --------------------------------------------------------------------------- CSV 解析
def parse_rows(kind: str, rows: Iterable) -> tuple:
    """解析 CSV 行，返回 ``(typed_rows, errors)``。

    :param rows: :func:`read_csv` 的返回值（``[(行号, dict)]``）。
    :raises CSVError: 文件级错误（未知类型 / 缺必需列）。
    """
    parser = {
        'materials': _parse_material_price_row,
        'rates': _parse_process_rate_row,
        'customers': _parse_customer_row,
    }.get(kind)
    if parser is None:
        raise CSVError(f'未知的导入类型：{kind}')

    parsed = []
    errors = []
    seen = set()
    for line, raw in _canonical_rows(kind, rows):
        try:
            row = parser(raw)
        except _RowError as exc:
            errors.append(CSVRowError(line, exc.message, exc.field))
            continue
        key = _row_key(kind, row)
        if key in seen:
            errors.append(
                CSVRowError(line, f'与前面的行重复（{key}），已跳过', _key_field(kind))
            )
            continue
        seen.add(key)
        parsed.append(row)
    return parsed, errors


def parse_csv_text(kind: str, text: str) -> tuple:
    """直接解析 CSV 文本，返回 ``(typed_rows, errors)``。"""
    return parse_rows(kind, read_csv(io.StringIO(text)))


def _row_key(kind: str, row: dict):
    if kind == 'materials':
        return f'{row["material_code"]} @ {row["effective_from"].isoformat()}'
    return row['code']


def _key_field(kind: str) -> str:
    if kind == 'materials':
        return 'effective_from'
    return 'code'


def _require(raw: dict, name: str, label: str = '') -> str:
    value = raw.get(name)
    text = '' if value is None else str(value).strip()
    if not text:
        raise _RowError('不能为空', name)
    return text


def _optional(raw: dict, name: str) -> str:
    value = raw.get(name)
    return '' if value is None else str(value).strip()


def _parse_date(value: str, field_name: str) -> date:
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise _RowError(f'日期格式非法：{text!r}（用 YYYY-MM-DD）', field_name)


def _parse_decimal(
    value,
    field_name: str,
    *,
    required: bool = True,
    min_value: Optional[Decimal] = None,
    max_value: Optional[Decimal] = None,
    places: int = 2,
) -> Decimal:
    text = '' if value is None else str(value).strip()
    if not text:
        if required:
            raise _RowError('不能为空', field_name)
        return Decimal('0')
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise _RowError(f'不是合法数字：{text!r}', field_name) from None
    if not number.is_finite():
        raise _RowError(f'不是有限数字：{text!r}', field_name)
    quantum = Decimal(1).scaleb(-places)
    if number != number.quantize(quantum):
        raise _RowError(f'最多 {places} 位小数：{text!r}', field_name)
    number = number.quantize(quantum)
    if min_value is not None and number < min_value:
        raise _RowError(f'必须 ≥ {min_value}：{number}', field_name)
    if max_value is not None and number > max_value:
        raise _RowError(f'必须 ≤ {max_value}：{number}', field_name)
    return number


def _parse_source(value: str) -> str:
    text = str(value or '').strip()
    if not text:
        return 'manual'
    code = SOURCE_ALIASES.get(text.lower(), SOURCE_ALIASES.get(text))
    if code is None:
        raise _RowError(
            f'未知来源：{text!r}（可选：{", ".join(SOURCE_CODES)} 或 采购/市场/手工/演示）',
            'source',
        )
    return code


def _parse_material_price_row(raw: dict) -> dict:
    material_code = _require(raw, 'material_code')
    material = Material.objects.filter(code=material_code).first()
    if material is None:
        raise _RowError(
            f'材质不存在：{material_code!r}，请先维护材质主数据（可用 seed_demo）',
            'material_code',
        )
    price = _parse_decimal(
        raw.get('price_per_kg'), 'price_per_kg', min_value=Decimal('0.01')
    )
    currency = (_optional(raw, 'currency') or 'CNY').upper()
    if currency not in MATERIAL_CURRENCIES:
        raise _RowError(
            f'材料价币种必须是 CNY（当前 {currency}）', 'currency'
        )
    effective_from = _parse_date(_require(raw, 'effective_from'), 'effective_from')
    source = _parse_source(_optional(raw, 'source'))
    note = _optional(raw, 'note')[:200]
    return {
        'material': material,
        'material_code': material_code,
        'price_per_kg': price,
        'currency': currency,
        'effective_from': effective_from,
        'source': source,
        'note': note,
    }


def _parse_unit(value: str, field_name: str = 'unit') -> str:
    text = _require({'unit': value}, 'unit')
    code = UNIT_ALIASES.get(text.lower(), UNIT_ALIASES.get(text))
    if code is None:
        raise _RowError(
            f'未知单位：{text!r}（可选：per_piece/per_kg/percent/per_hour 或 元/件、元/kg、%、元/时）',
            field_name,
        )
    return code


def _parse_tiers(value, field_name: str = 'tiers') -> dict:
    text = str(value or '').strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise _RowError(
            '档位必须是 JSON 对象，如 {"<=50": 12, "<=100": 18}', field_name
        ) from None
    if not isinstance(data, dict):
        raise _RowError('档位必须是 JSON 对象（键 → 数值）', field_name)
    tiers = {}
    for key, raw_value in data.items():
        key_text = str(key).strip()
        if not key_text:
            raise _RowError('档位键不能为空', field_name)
        try:
            number = Decimal(str(raw_value))
        except (InvalidOperation, ValueError):
            raise _RowError(f'档位 {key_text!r} 的值不是数字：{raw_value!r}', field_name) from None
        if not number.is_finite() or number < 0:
            raise _RowError(f'档位 {key_text!r} 的值必须是非负有限数', field_name)
        tiers[key_text] = (
            int(number) if number == number.to_integral_value() else float(number)
        )
    return tiers


def _parse_process_rate_row(raw: dict) -> dict:
    code = _require(raw, 'code')
    if len(code) > 40 or not _SLUG_RE.match(code):
        raise _RowError('费率代码只能用字母/数字/下划线/短横，且不超过 40 字符', 'code')
    label = (_optional(raw, 'label') or code)[:60]
    unit = _parse_unit(_optional(raw, 'unit'))
    rate = _parse_decimal(raw.get('rate'), 'rate', min_value=Decimal('0'))
    tiers = _normalize_tiers(code, _parse_tiers(_optional(raw, 'tiers')))
    effective_from = _parse_date(_require(raw, 'effective_from'), 'effective_from')
    source = _parse_source(_optional(raw, 'source'))
    return {
        'code': code,
        'label': label,
        'unit': unit,
        'rate': rate,
        'tiers': tiers,
        'effective_from': effective_from,
        'source': source,
    }


def _normalize_tiers(code: str, tiers: dict) -> dict:
    """规范加工费档位键。

    契约 §2.3 的示例是 ``{"<=50": 12, "<=100": 18}``，但 ``services._resolve_rates``
    对 ``machining`` 档位按 ``int(limit)`` 转成 ``costing.machining_base`` 的累计
    上限列表，键必须是数字。这里把 ``<=50`` / ``<50`` / ``50`` 统一成 ``"50"`` 并按
    DN 升序存，避免导入一份“契约形状”的 CSV 后报价计算直接报错。
    """
    if code != 'machining':
        return tiers
    normalized = {}
    for key, value in tiers.items():
        text = str(key).strip()
        if text.startswith('<='):
            text = text[2:]
        elif text.startswith('<'):
            text = text[1:]
        text = text.replace('dn', '').replace('DN', '').strip()
        try:
            limit = int(float(text))
        except ValueError:
            raise _RowError(
                f'加工费档位键必须是 DN 数字（如 50、<=100），当前 {key!r}', 'tiers'
            ) from None
        if limit <= 0:
            raise _RowError(f'加工费档位 DN 必须大于 0：{key!r}', 'tiers')
        if str(limit) in normalized:
            raise _RowError(f'加工费档位重复：{key!r}', 'tiers')
        normalized[str(limit)] = value
    return {key: normalized[key] for key in sorted(normalized, key=int)}


def _parse_bool(value, field_name: str = 'active', default: bool = True) -> bool:
    text = str(value or '').strip().lower()
    if not text:
        return default
    if text in {'1', 'true', 'yes', 'y', 'on', '是', '启用', '启用中'}:
        return True
    if text in {'0', 'false', 'no', 'n', 'off', '否', '停用', '禁用'}:
        return False
    raise _RowError(f'布尔值非法：{value!r}（用 true/false 或 1/0）', field_name)


def _parse_customer_row(raw: dict) -> dict:
    code = _require(raw, 'code')
    if len(code) > 40 or not _SLUG_RE.match(code):
        raise _RowError('客户编码只能用字母/数字/下划线/短横，且不超过 40 字符', 'code')
    name = _require(raw, 'name')[:120]
    level = (_optional(raw, 'level') or 'B').upper()
    if level not in CUSTOMER_LEVELS:
        raise _RowError(f'客户等级必须是 A/B/C（当前 {level!r}）', 'level')
    discount = _parse_decimal(
        raw.get('discount_pct'),
        'discount_pct',
        required=False,
        min_value=CUSTOMER_DISCOUNT_MIN,
        max_value=CUSTOMER_DISCOUNT_MAX,
    )
    currency = (_optional(raw, 'currency') or 'CNY').upper()
    if currency not in CURRENCIES:
        raise _RowError(f'客户币种必须是 CNY/USD（当前 {currency!r}）', 'currency')
    payment_terms = _optional(raw, 'payment_terms')[:100]
    active = _parse_bool(_optional(raw, 'active'))
    return {
        'code': code,
        'name': name,
        'level': level,
        'discount_pct': discount,
        'currency': currency,
        'payment_terms': payment_terms,
        'active': active,
    }


# --------------------------------------------------------------------------- CSV 写库
def _material_price_defaults(row: dict) -> dict:
    return {
        'price_per_kg': row['price_per_kg'],
        'currency': row['currency'],
        'source': row['source'],
        'note': row['note'],
    }


def _process_rate_defaults(row: dict) -> dict:
    return {
        'label': row['label'],
        'unit': row['unit'],
        'rate': row['rate'],
        'tiers': row['tiers'],
        'effective_from': row['effective_from'],
        'source': row.get('source') or 'manual',
    }


def _row_exists(kind: str, row: dict) -> bool:
    if kind == 'materials':
        return MaterialPrice.objects.filter(
            material=row['material'], effective_from=row['effective_from']
        ).exists()
    if kind == 'rates':
        return ProcessRate.objects.filter(code=row['code']).exists()
    return Customer.objects.filter(code=row['code']).exists()


def _apply_rows(kind: str, rows: Sequence[dict]) -> tuple:
    created = updated = 0
    for row in rows:
        if kind == 'materials':
            _, is_created = MaterialPrice.objects.update_or_create(
                material=row['material'],
                effective_from=row['effective_from'],
                defaults=_material_price_defaults(row),
            )
        elif kind == 'rates':
            _, is_created = ProcessRate.objects.update_or_create(
                code=row['code'], defaults=_process_rate_defaults(row)
            )
        else:
            _, is_created = Customer.objects.update_or_create(
                code=row['code'],
                defaults={
                    'name': row['name'],
                    'level': row['level'],
                    'discount_pct': row['discount_pct'],
                    'currency': row['currency'],
                    'payment_terms': row['payment_terms'],
                    'active': row['active'],
                },
            )
        if is_created:
            created += 1
        else:
            updated += 1
    return created, updated


def import_rows(
    kind: str,
    rows: Iterable,
    *,
    dry_run: bool = False,
    strict: bool = False,
) -> ImportResult:
    """解析并写入 CSV 行（幂等）。

    - ``strict=True``：任何一行有错误则整批不写，返回带 errors 的结果；
    - ``dry_run=True``：只解析/统计，不写库；
    - 默认：写合法行，错误行逐行返回（由调用方决定是否报错退出）。
    """
    typed, errors = parse_rows(kind, rows)
    result = ImportResult(kind=kind, row_count=len(typed) + len(errors), errors=errors)
    if strict and errors:
        return result
    if dry_run:
        existing = sum(1 for row in typed if _row_exists(kind, row))
        result.created = len(typed) - existing
        result.updated = existing
        return result
    with transaction.atomic():
        result.created, result.updated = _apply_rows(kind, typed)
    result.applied = True
    return result


def import_csv(
    kind: str,
    source,
    *,
    dry_run: bool = False,
    strict: bool = False,
    encoding: str = 'utf-8-sig',
) -> ImportResult:
    """读取 CSV 文件（或流）并导入；返回 :class:`ImportResult`。"""
    return import_rows(
        kind,
        read_csv(source, encoding=encoding),
        dry_run=dry_run,
        strict=strict,
    )


def csv_template_text(kind: str) -> str:
    """返回某类导入的 CSV 模板文本（表头 + 一行示例）。"""
    if kind not in CSV_KINDS:
        raise CSVError(f'未知的导入类型：{kind}（可选：{", ".join(sorted(CSV_KINDS))}）')
    spec = CSV_KINDS[kind]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(spec['columns'])
    writer.writerow(spec['example'])
    return buffer.getvalue()


__all__ = [
    'CSVError',
    'CSVRowError',
    'CSV_KINDS',
    'CUSTOMER_COLUMNS',
    'DEMO_SOURCE',
    'DEMO_SOURCE_CHOICE',
    'DEMO_SOURCE_LABEL',
    'ImportResult',
    'MATERIAL_PRICE_COLUMNS',
    'PROCESS_RATE_COLUMNS',
    'PriceError',
    'SOURCE_LABELS',
    'all_rates',
    'csv_template_text',
    'customer_discount_pct',
    'discount_source',
    'import_csv',
    'import_rows',
    'material_prices_on',
    'parse_csv_text',
    'parse_rows',
    'price_for',
    'rate_for',
    'read_csv',
    'resolve_discount',
]
