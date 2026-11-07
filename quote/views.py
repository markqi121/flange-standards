"""法兰报价 · 服务端渲染 UI（T2）。

页面：
- /quote/                工作台（选型 → 材质/数量 → 工艺/检测 → 定价 → A4 预览）
- /quote/list/           报价单列表
- /quote/<number>/       报价单详情
- /quote/<number>/print/ A4 打印页（只输出报价单）
- /quote/<number>/pdf/   服务端 PDF（Playwright，缺依赖时优雅降级）
- /quote/customers/       客户（读 DB，只读）
- /quote/prices/         价格库（读 DB，只读）

集成状态（2026-11-06，T1 已完成）：
- 成本/重量/定价/校验一律调用 quote/api.py 的 POST /quote/calc/，UI 不实现第二套算法；
- 创建调用 POST /quote/create/，成功后跳转服务端详情页；计算/创建失败原样展示
  `{ok:false, error, message, field}`（契约 §8）；
- 列表/详情/打印读取真实 Quotation；仅当 T1 模型或迁移不可用时才使用 MOCK_QUOTES 样张；
  `?demo=1` 可强制使用样张，方便 UI 评审与自动化测试；
- 前端保留的“契约夹具”只在 /quote/calc/ 未挂载/网络不可达时用于版式预览，并明确标注离线。
"""

import datetime
import logging
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.http import FileResponse, Http404, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from urllib.parse import quote as urlquote

from django.urls import reverse

from catalog.models import DimensionRow, FlangeType, Standard

from . import permissions
from .excel import build_quote_workbook, build_quotes_workbook
from .pdf import print_page_token, render_quote_pdf, verify_print_page_token

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 静态示例夹具（来源：docs/报价功能-接口契约.md §3.2 / §7 / §9；
# 材质主数据来自 docs/法兰报价系统设计方案.html 原型数据，价格均为“示例”，禁止用于生产）
# ---------------------------------------------------------------------------

MATERIALS = [
    {'code': 'A105', 'label': 'ASTM A105 / A105N', 'family': '碳钢', 'density': 7.85, 'demo_price': 7.20, 'pt': '1.1'},
    {'code': 'LF2', 'label': 'ASTM A350 LF2', 'family': '低温碳钢', 'density': 7.85, 'demo_price': 8.60, 'pt': '1.1'},
    {'code': 'F304', 'label': 'ASTM A182 F304', 'family': '不锈钢 304', 'density': 7.93, 'demo_price': 28.00, 'pt': '2.1'},
    {'code': 'F316L', 'label': 'ASTM A182 F316L', 'family': '不锈钢 316L', 'density': 7.93, 'demo_price': 35.00, 'pt': '2.3'},
    {'code': 'F11', 'label': 'ASTM A182 F11', 'family': '铬钼合金钢', 'density': 7.85, 'demo_price': 10.20, 'pt': '1.9'},
    {'code': 'F22', 'label': 'ASTM A182 F22', 'family': '铬钼合金钢', 'density': 7.85, 'demo_price': 12.00, 'pt': '1.10'},
    {'code': 'F91', 'label': 'ASTM A182 F91', 'family': '铬钼合金钢', 'density': 7.85, 'demo_price': 16.00, 'pt': '1.15'},
    {'code': 'F51', 'label': 'ASTM A182 F51 (双相)', 'family': '双相不锈钢', 'density': 7.90, 'demo_price': 62.00, 'pt': None},
    {'code': 'A694F52', 'label': 'ASTM A694 F52', 'family': '碳钢', 'density': 7.85, 'demo_price': 10.50, 'pt': None},
    {'code': 'A694F60', 'label': 'ASTM A694 F60', 'family': '碳钢', 'density': 7.85, 'demo_price': 11.50, 'pt': None},
    {'code': 'P250GH', 'label': 'P250GH', 'family': '碳钢', 'density': 7.85, 'demo_price': 7.00, 'pt': None},
    {'code': 'P265GH', 'label': 'P265GH', 'family': '碳钢', 'density': 7.85, 'demo_price': 7.20, 'pt': None},
    {'code': 'S235JR', 'label': 'S235JR', 'family': '碳钢', 'density': 7.85, 'demo_price': 6.60, 'pt': None},
    {'code': 'S355J2', 'label': 'S355J2', 'family': '低合金钢', 'density': 7.85, 'demo_price': 7.80, 'pt': None},
    {'code': '1.4301', 'label': '1.4301 (304)', 'family': '不锈钢 304', 'density': 7.93, 'demo_price': 27.00, 'pt': None},
    {'code': '1.4404', 'label': '1.4404 (316L)', 'family': '不锈钢 316L', 'density': 7.93, 'demo_price': 34.00, 'pt': None},
    {'code': '1.4571', 'label': '1.4571 (316Ti)', 'family': '不锈钢 316Ti', 'density': 7.93, 'demo_price': 38.00, 'pt': None},
    {'code': 'SS400', 'label': 'SS400', 'family': '碳钢', 'density': 7.85, 'demo_price': 6.60, 'pt': None},
    {'code': 'S25C', 'label': 'S25C', 'family': '碳钢', 'density': 7.85, 'demo_price': 7.20, 'pt': None},
    {'code': 'SUS304', 'label': 'SUS304', 'family': '不锈钢 304', 'density': 7.93, 'demo_price': 27.00, 'pt': None},
    {'code': 'SUS316', 'label': 'SUS316', 'family': '不锈钢 316', 'density': 7.93, 'demo_price': 34.00, 'pt': None},
    {'code': 'SCS13', 'label': 'SCS13 (铸件)', 'family': '不锈钢 304', 'density': 7.93, 'demo_price': 30.00, 'pt': None},
    {'code': 'SCS14', 'label': 'SCS14 (铸件)', 'family': '不锈钢 316', 'density': 7.93, 'demo_price': 38.00, 'pt': None},
]
MATERIAL_BY_CODE = {m['code']: m for m in MATERIALS}

# 每个标准可选的材质牌号（与 catalog.Standard.material_options 对应，来自设计原型静态数据）
STANDARD_MATERIALS = {
    'asme-b16-5': ['A105', 'LF2', 'F304', 'F316L', 'F11', 'F22', 'F91', 'F51'],
    'asme-b16-47-a': ['A105', 'LF2', 'F304', 'F316L', 'F51', 'A694F52', 'A694F60'],
    'asme-b16-47-b': ['A105', 'LF2', 'F304', 'F316L', 'F51', 'A694F52', 'A694F60'],
    'en-1092-1': ['P250GH', 'P265GH', 'S235JR', 'S355J2', '1.4301', '1.4404', '1.4571'],
    'jis-b2220': ['SS400', 'S25C', 'SUS304', 'SUS316', 'SCS13', 'SCS14'],
}

DEFAULT_PARAMS = {
    'yield': 0.70,
    'machining_factor': 1.0,
    'management_pct': 6,
    'profit_pct': 12,
    'tax_pct': 13,
}
DEFAULT_QUOTE = {
    'std': 'asme-b16-5',
    'type': 'weld-neck',
    'rating': '150',
    'size': '4',
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
}

# 契约 §9 的默认样板：4″ Class 150 WN A105（夹具输入必须与 UI 默认值完全一致）
CONTRACT_FIXTURE = {
    'key': (
        'asme-b16-5|weld-neck|4|150|A105|10|38|16|none|wood|'
        '|0.6|0|CNY|7.1|0.7|1|6|12|13'
    ),
    'label': '契约 §9 夹具 · 4″ Class 150 WN A105 · 数量 10',
    'source': 'contract',
    'input': {
        'std': 'asme-b16-5', 'type': 'weld-neck', 'size': '4', 'rating': '150',
        'material': 'A105', 'qty': 10, 'design_temp_c': 38, 'design_pressure_bar': 16,
        'surface': 'none', 'packaging': 'wood', 'inspections': [],
        'freight_per_kg': 0.6, 'discount_pct': 0, 'currency': 'CNY', 'fx_rate': 7.10,
        'params': dict(DEFAULT_PARAMS),
    },
    'response': {
        'ok': True,
        'item': {
            'description': 'ASME B16.5 Class 150 4″ 对焊法兰（WN）',
            'standard_edition': 'ASME B16.5-2025',
            'material': 'ASTM A105 / A105N',
            'size_label': '4″',
            'rating_label': 'Class 150',
            'qty': 10,
            'net_weight': 6.800,
            'weight_source': 'standard',
            'weight_confidence': 'B',
            'weight_rule': 'w0.3',
            'lead_time_days': 8,
        },
        'costs': {
            'material': 69.94, 'machining': 20.70, 'surface': 0, 'inspection': 0,
            'packaging': 35, 'freight': 4.08, 'management': 7.78, 'total': 137.51,
        },
        'price': {
            'unit_net': 150.93, 'unit_tax': 19.62, 'unit_gross': 170.55,
            'amount_net': 1509.30, 'tax_amount': 196.20, 'total_gross': 1705.50,
            'margin_pct': 8.9, 'per_kg': 25.08,
        },
        'validation': {
            'status': 'pass',
            'messages': [],
            'pt': {'status': 'pass', 'allowed_bar': 19.6, 'material_group': '1.1'},
        },
        'approval_required': False,
        'snapshot': {
            'params': dict(DEFAULT_PARAMS),
            'standard_editions': {'asme-b16-5': 'ASME B16.5-2025'},
            'price_versions': {
                'material_price': '示例（2026-01-01 生效）',
                'process_rates': '示例（2026-01-01 生效）',
            },
            'weight_rule': 'w0.3',
            'fx_rate': 7.10,
            'tax_rate': 13,
        },
    },
}

# 契约 §9 的其余重量夹具：只有重量，没有成本/价格 → JS 显示“等待 T1 计算接口”
CONTRACT_WEIGHT_FIXTURES = [
    {
        'key': 'asme-b16-5|slip-on|4|150|A105|10|38|16|none|wood||0.6|0|CNY|7.1|0.7|1|6|12|13',
        'label': '契约 §9 夹具 · 4″ Class 150 SO A105（仅重量）',
        'source': 'contract',
        'input': {
            'std': 'asme-b16-5', 'type': 'slip-on', 'size': '4', 'rating': '150',
            'material': 'A105', 'qty': 10, 'design_temp_c': 38, 'design_pressure_bar': 16,
            'surface': 'none', 'packaging': 'wood', 'inspections': [],
            'freight_per_kg': 0.6, 'discount_pct': 0, 'currency': 'CNY', 'fx_rate': 7.10,
            'params': dict(DEFAULT_PARAMS),
        },
        'response': {
            'ok': True,
            'item': {
                'description': 'ASME B16.5 Class 150 4″ 滑套法兰（SO）',
                'standard_edition': 'ASME B16.5-2025',
                'material': 'ASTM A105 / A105N',
                'size_label': '4″', 'rating_label': 'Class 150', 'qty': 10,
                'net_weight': 5.900, 'weight_source': 'standard',
                'weight_confidence': 'B', 'weight_rule': 'w0.3', 'lead_time_days': 8,
            },
            'partial': True,
            'message': '契约仅提供该组合的重量夹具；成本、价格与 p/T 校验等待 T1 /quote/calc/ 接口。',
        },
    },
    {
        'key': 'asme-b16-5|blind|4|150|A105|10|38|16|none|wood||0.6|0|CNY|7.1|0.7|1|6|12|13',
        'label': '契约 §9 夹具 · 4″ Class 150 BL A105（仅重量）',
        'source': 'contract',
        'input': {
            'std': 'asme-b16-5', 'type': 'blind', 'size': '4', 'rating': '150',
            'material': 'A105', 'qty': 10, 'design_temp_c': 38, 'design_pressure_bar': 16,
            'surface': 'none', 'packaging': 'wood', 'inspections': [],
            'freight_per_kg': 0.6, 'discount_pct': 0, 'currency': 'CNY', 'fx_rate': 7.10,
            'params': dict(DEFAULT_PARAMS),
        },
        'response': {
            'ok': True,
            'item': {
                'description': 'ASME B16.5 Class 150 4″ 盲板法兰（BL）',
                'standard_edition': 'ASME B16.5-2025',
                'material': 'ASTM A105 / A105N',
                'size_label': '4″', 'rating_label': 'Class 150', 'qty': 10,
                'net_weight': 7.700, 'weight_source': 'standard',
                'weight_confidence': 'B', 'weight_rule': 'w0.3', 'lead_time_days': 8,
            },
            'partial': True,
            'message': '契约仅提供该组合的重量夹具；成本、价格与 p/T 校验等待 T1 /quote/calc/ 接口。',
        },
    },
]

STATUS_LABELS = {
    'draft': '草稿',
    'pending': '待审批',
    'pending_approval': '待审批',
    'approved': '已批准',
    'sent': '已发送',
    'accepted': '已接受',
    'rejected': '已拒绝',
    'expired': '已过期',
    'cancelled': '已作废',
    'ordered': '已转订单',
}
SURFACE_LABELS = {'none': '不做处理', 'rust': '防锈油', 'zinc': '热镀锌', 'paint': '喷涂'}
PACKAGING_LABELS = {'none': '不包装', 'carton': '纸箱', 'wood': '木箱', 'fumigated': '熏蒸木箱'}
INSPECTION_LABELS = {'pmi': 'PMI 光谱', 'ut': 'UT 探伤', 'hydro': '水压试验', 'hardness': '硬度检测', 'third': '第三方见证'}

STATUS_TONES = {
    'draft': 'neutral',
    'pending': 'warn',
    'pending_approval': 'warn',
    'approved': 'info',
    'sent': 'info',
    'accepted': 'ok',
    'ordered': 'ok',
    'rejected': 'bad',
    'expired': 'neutral',
    'cancelled': 'bad',
}


# 状态机可用动作（与 quote/services.py transition() 对齐；T1 负责执行与校验）
STATUS_ACTIONS = {
    'draft': ['submit', 'cancel'],
    'pending': ['approve', 'reject', 'cancel'],
    'approved': ['send', 'expire', 'cancel'],
    'sent': ['accept', 'reject', 'expire', 'cancel'],
    'accepted': ['order', 'cancel'],
    'rejected': ['cancel'],
    'expired': ['cancel'],
    'ordered': [],
    'cancelled': [],
}
ACTION_LABELS = {
    'submit': '提交审批',
    'approve': '批准',
    'reject': '拒绝',
    'send': '标记已发送',
    'accept': '客户接受',
    'order': '转订单',
    'expire': '标记过期',
    'cancel': '作废',
}
ACTION_CSS = {
    'submit': 'btn-primary',
    'approve': 'btn-primary',
    'send': 'btn-primary',
    'accept': 'btn-primary',
    'order': 'btn-primary',
    'reject': 'action-danger',
    'expire': '',
    'cancel': 'action-danger',
}
ACTION_CONFIRM = {'reject', 'cancel', 'expire'}
ACTION_COMMENT = {'reject', 'cancel'}


def _quote_actions(status, demo=False):
    if demo:
        return []
    return [
        {
            'action': action,
            'label': ACTION_LABELS.get(action, action),
            'css': ACTION_CSS.get(action, ''),
            'confirm': action in ACTION_CONFIRM,
            'comment': action in ACTION_COMMENT,
        }
        for action in STATUS_ACTIONS.get(status, [])
    ]


# ---------------------------------------------------------------------------
# 访问控制（契约 §5：未登录不开放 /quote/*；登录入口为 settings.LOGIN_URL）
# ---------------------------------------------------------------------------

def quote_login_required(view):
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view(request, *args, **kwargs)
        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)

    return _wrapped


# ---------------------------------------------------------------------------
# Excel 导出
# ---------------------------------------------------------------------------

XLSX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _xlsx_response(content, filename):
    response = HttpResponse(content, content_type=XLSX_CONTENT_TYPE)
    ascii_name = filename.encode('ascii', 'ignore').decode('ascii').strip() or 'quote.xlsx'
    if ascii_name == filename:
        response['Content-Disposition'] = f'attachment; filename="{ascii_name}"'
    else:
        response['Content-Disposition'] = (
            f"attachment; filename*=UTF-8''{urlquote(filename)}"
        )
    return response


def _export_generated_at():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M')


def _quote_include_cost(user):
    """Excel 成本区块与详情页一致：必须由明确角色授权（兼容模式不输出成本）。"""
    return permissions.can_view_cost(user) and permissions.role_for(user) is not None


def _prepare_export_quote(quote, include_cost):
    if not include_cost:
        quote['cost_breakdown'] = None
        quote['snapshot_meta'] = {}
    quote['upper'] = (
        rmb_upper(quote.get('total'))
        if str(quote.get('currency') or 'CNY').upper() != 'USD' else ''
    )
    return quote


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

_CN_DIGITS = '零壹贰叁肆伍陆柒捌玖'


def rmb_upper(amount):
    """人民币金额大写（仅用于报价单展示，不参与任何成本/定价计算）。"""
    if amount is None:
        return '—'
    value = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if value == 0:
        return '零元整'
    negative = value < 0
    value = abs(value)
    integer = int(value)
    jiao = int((value * 10) % 10)
    fen = int((value * 100) % 10)

    def _four(group):
        text = ''
        zero = False
        for index, char in enumerate(f'{group:04d}'):
            digit = int(char)
            if digit == 0:
                zero = True
                continue
            if zero and text:
                text += '零'
            text += _CN_DIGITS[digit] + ('', '拾', '佰', '仟')[3 - index]
            zero = False
        return text

    groups = []
    while integer > 0:
        groups.append(integer % 10000)
        integer //= 10000
    result = ''
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if group == 0:
            if result and not result.endswith('零'):
                result += '零'
            continue
        if result and group < 1000 and not result.endswith('零'):
            result += '零'
        result += _four(group) + ('', '万', '亿', '万亿')[index]
    result = result.rstrip('零') or '零'

    if jiao == 0 and fen == 0:
        tail = '整'
    else:
        tail = ''
        if jiao:
            tail += _CN_DIGITS[jiao] + '角'
        elif result != '零':
            tail += '零'
        if fen:
            tail += _CN_DIGITS[fen] + '分'
    return ('负' if negative else '') + result + '元' + tail


def _money(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (TypeError, ValueError):
        return None


def _display_amount(value, currency='CNY', fx_rate=7.10, digits='0.01'):
    """报价金额展示换算：后端金额按 CNY 计算，USD 报价在展示层按 fx_rate 折算。"""
    amount = _money(value)
    if amount is None:
        return None
    if str(currency).upper() == 'USD':
        rate = Decimal(str(fx_rate or 7.10))
        if rate <= 0:
            rate = Decimal('7.10')
        return (amount / rate).quantize(Decimal(digits), rounding=ROUND_HALF_UP)
    return amount


def _currency_symbol(currency):
    return 'US$' if str(currency).upper() == 'USD' else '¥'


def _float(value):
    try:
        return float(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _status_label(code):
    return STATUS_LABELS.get(code, code or '—')


def _status_tone(code):
    return STATUS_TONES.get(code, 'neutral')


def _weight_source_label(source):
    return {
        'standard': '标准参考重量（B 级）',
        'geometry': '几何估算（C 级）',
        'manual': '人工复核',
    }.get(source, source or '—')


def _date(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 工作台
# ---------------------------------------------------------------------------

SOURCE_LABELS = {'purchase': '采购', 'market': '市场', 'manual': '手工', 'demo': '演示'}


def _db_materials():
    """从 T3 价格库读取材质 + 最近一条材料价；表未就绪时返回空列表。"""
    try:
        from .models import Material, MaterialPrice

        rows = []
        for material in Material.objects.filter(active=True).order_by('sort_order', 'code'):
            latest = (
                MaterialPrice.objects.filter(material=material)
                .order_by('-effective_from', '-id')
                .first()
            )
            rows.append({
                'code': material.code,
                'label': material.label,
                'family': material.family,
                'density': _float(material.density),
                'demo_price': _float(latest.price_per_kg) if latest else None,
                'price_source': getattr(latest, 'source', '') if latest else '',
                'price_source_label': SOURCE_LABELS.get(getattr(latest, 'source', ''), '—') if latest else '—',
                'price_effective_from': latest.effective_from if latest else None,
                'price_currency': getattr(latest, 'currency', 'CNY') if latest else 'CNY',
                'price_is_demo': bool(latest and getattr(latest, 'source', '') == 'demo'),
            })
        return rows
    except Exception:  # pragma: no cover - 迁移未执行时的兜底
        logger.debug('读取材质价格库失败', exc_info=True)
        return []


def _db_customers():
    try:
        from .models import Customer

        return [
            {
                'code': customer.code,
                'name': customer.name,
                'level': customer.level,
                'discount_pct': _float(customer.discount_pct),
                'currency': customer.currency,
                'payment_terms': customer.payment_terms or '',
                'is_demo': '演示' in (customer.name or ''),
            }
            for customer in Customer.objects.filter(active=True).order_by('code')
        ]
    except Exception:  # pragma: no cover
        logger.debug('读取客户失败', exc_info=True)
        return []


def _standard_material_codes(standard_id, available=None):
    codes = STANDARD_MATERIALS.get(standard_id, [])
    if available is not None:
        codes = [code for code in codes if code in available]
    else:
        codes = [code for code in codes if code in MATERIAL_BY_CODE]
    return codes


def _workbench_bootstrap(request):
    standards = list(Standard.objects.prefetch_related('ratings').all())
    types = list(FlangeType.objects.all())
    db_materials = _db_materials()
    materials_payload = db_materials or MATERIALS
    can_view_cost = permissions.can_view_cost(request.user)
    if not can_view_cost:
        # 销售等无成本权限角色：只保留选型需要的牌号/密度，不注入材料价
        materials_payload = [
            {
                'code': item['code'],
                'label': item['label'],
                'family': item['family'],
                'density': item.get('density'),
                'demo_price': None,
                'price_source': '',
                'price_source_label': '无权限',
                'price_effective_from': None,
                'price_currency': 'CNY',
                'price_is_demo': False,
            }
            for item in materials_payload
        ]
    material_codes = {item['code'] for item in materials_payload}
    db_customers = _db_customers()
    rows = list(
        DimensionRow.objects.values_list(
            'id', 'standard_id', 'flange_type_id', 'size', 'size_label', 'dn', 'rating__code',
            'od', 'th', 'rf', 'rf_height', 'bc', 'bolts', 'bolt_hole', 'hub', 'length',
            'hub_weld', 'weight',
        )
    )
    type_order = {item.slug: item.sort_order for item in types}
    std_payload = []
    for standard in standards:
        rating_pairs = [[rating.code, rating.label] for rating in standard.ratings.all()]
        type_codes = sorted(
            {row[2] for row in rows if row[1] == standard.pk},
            key=lambda code: (type_order.get(code, 99), code),
        )
        std_payload.append({
            'id': standard.pk,
            'name': standard.name,
            'short': standard.short,
            'edition': standard.effective_edition,
            'types': type_codes,
            'ratings': rating_pairs,
            'materials': _standard_material_codes(standard.pk, material_codes),
        })

    row_payload = [
        [
            row[1], row[2], row[3], row[4], row[5], row[6],
            _float(row[7]), _float(row[8]), _float(row[9]), _float(row[10]),
            _float(row[11]), row[12], _float(row[13]), _float(row[14]), _float(row[15]),
            _float(row[16]), _float(row[17]), row[0],
        ]
        for row in rows
    ]

    today = datetime.date.today()
    bootstrap = {
        'standards': std_payload,
        'types': [{'code': item.slug, 'name': item.name_zh, 'en': item.name_en} for item in types],
        'materials': materials_payload,
        'customers': db_customers,
        'rows': row_payload,
        'defaults': dict(DEFAULT_QUOTE),
        'params': dict(DEFAULT_PARAMS),
        'fixtures': [CONTRACT_FIXTURE, *CONTRACT_WEIGHT_FIXTURES] if can_view_cost else [],
        'endpoints': {
            # T1（quote/api.py）已挂载：/quote/options/、/quote/calc/、/quote/create/。
            'calc': reverse('quote:api_calc'),
            'options': reverse('quote:api_options'),
            'create': reverse('quote:api_create'),
            'workbench': reverse('quote:index'),
        },
        'today': today.isoformat(),
        'valid_until': (today + datetime.timedelta(days=15)).isoformat(),
        'csrf_token': get_token(request),
        'can_create': permissions.can_create_quote(request.user),
        'can_view_cost': permissions.can_view_cost(request.user),
        'note': '示例参数仅用于界面演示；成本、价格与 p/T 校验以 T1 后端 /quote/calc/ 返回为准。',
    }
    return bootstrap, standards, types


@quote_login_required
def workbench(request):
    if not permissions.legacy_mode() and permissions.role_for(request.user) is None:
        return _forbidden(request, '当前账号未分配报价角色，不能进入工作台；请联系管理员配置角色组。')
    bootstrap, standards, types = _workbench_bootstrap(request)
    default_std = next(
        (item for item in bootstrap['standards'] if item['id'] == DEFAULT_QUOTE['std']),
        bootstrap['standards'][0] if bootstrap['standards'] else None,
    )
    default_ratings = default_std['ratings'] if default_std else []
    default_types = [t for t in bootstrap['types'] if default_std and t['code'] in default_std['types']]
    material_by_code = {item['code']: item for item in bootstrap['materials']}
    default_materials = [
        material_by_code[code] for code in (default_std['materials'] if default_std else [])
        if code in material_by_code
    ]
    default_sizes = [
        row for row in bootstrap['rows']
        if row[0] == DEFAULT_QUOTE['std']
        and row[1] == DEFAULT_QUOTE['type']
        and row[5] == DEFAULT_QUOTE['rating']
    ]
    default_sizes.sort(key=lambda row: row[4])
    sample_quote = _build_sample_quote()
    context = {
        'page': 'workbench',
        'bootstrap': bootstrap,
        'standards': standards,
        'types': types,
        'default_std': default_std,
        'default_ratings': default_ratings,
        'default_types': default_types,
        'default_materials': default_materials,
        'default_sizes': default_sizes,
        'default_params': DEFAULT_PARAMS,
        'default_quote': DEFAULT_QUOTE,
        'default_material': material_by_code.get(DEFAULT_QUOTE['material'])
        or (bootstrap['materials'][0] if bootstrap['materials'] else None),
        'material_list': bootstrap['materials'],
        'customers': bootstrap['customers'],
        'sample_quote': sample_quote,
        'sample_upper': rmb_upper(sample_quote['total']),
        'status_labels': STATUS_LABELS,
        'contract_fixture_label': CONTRACT_FIXTURE['label'],
        'can_create_quote': permissions.can_create_quote(request.user),
        'can_view_cost': permissions.can_view_cost(request.user),
        'role_label': _role_label(request.user),
    }
    return render(request, 'quote/workbench.html', context)


# ---------------------------------------------------------------------------
# 列表 / 详情 / 打印 / PDF
# ---------------------------------------------------------------------------

def _real_model_available():
    """T1 模型与迁移是否可用；不可用时返回 False，页面使用静态样张。"""
    try:
        from .models import Quotation
    except Exception:  # pragma: no cover - T1 未完成时的正常分支
        return False
    try:
        Quotation.objects.count()
    except DatabaseError:
        return False
    except Exception:  # pragma: no cover - 模型字段/查询异常的兜底
        return False
    return True


def _real_quotation_queryset(user=None):
    from .models import Quotation

    base = permissions.visible_quotations(user) if user is not None else Quotation.objects.all()
    return base.select_related('customer', 'created_by', 'approved_by').prefetch_related(
        'items__dimension_row__standard',
        'items__dimension_row__flange_type',
        'items__dimension_row__rating',
        'items__material',
    )


def _find_quotation_model(number):
    if not _real_model_available():
        return None
    try:
        return _real_quotation_queryset().filter(number=number).first()
    except Exception:  # pragma: no cover - 字段差异兜底
        logger.warning('读取报价单模型失败：%s', number, exc_info=True)
        return None


def _role_label(user):
    role = permissions.role_for(user)
    if role:
        return permissions.ROLE_LABELS.get(role, role)
    return '兼容模式（未分配角色）' if permissions.legacy_mode() else '未分配角色'


def _forbidden(request, message, quote=None):
    return render(request, 'quote/forbidden.html', {
        'page': 'forbidden',
        'message': message,
        'quote': quote,
        'role_label': _role_label(request.user),
        'scope_label': permissions.quote_scope_label(request.user),
    }, status=403)


def _process_summary(process):
    if not process:
        return '—'
    if isinstance(process, dict):
        parts = []
        surface = process.get('surface') or process.get('surface_label')
        packaging = process.get('packaging') or process.get('packaging_label')
        inspections = process.get('inspections') or []
        if surface:
            parts.append(f'表面 {SURFACE_LABELS.get(str(surface), surface)}')
        if packaging:
            parts.append(f'包装 {PACKAGING_LABELS.get(str(packaging), packaging)}')
        if isinstance(inspections, (list, tuple)):
            labels = [INSPECTION_LABELS.get(str(item), str(item)) for item in inspections]
            if labels:
                parts.append(f'检测 {" / ".join(labels)}')
        elif inspections:
            parts.append(f'检测 {inspections}')
        return ' · '.join(parts) if parts else '—'
    return str(process)[:120]


def _real_quote_to_dict(quotation, user=None):
    items = []
    item_subtotal = Decimal('0')
    cost_breakdown = None
    include_cost = user is not None and permissions.can_view_cost(user)
    currency = getattr(quotation, 'currency', 'CNY') or 'CNY'
    fx_rate = _float(getattr(quotation, 'fx_rate', 7.10)) or 7.10
    symbol = _currency_symbol(currency)
    for index, item in enumerate(quotation.items.all()):
        row = getattr(item, 'dimension_row', None)
        material = getattr(item, 'material', None)
        standard = getattr(row, 'standard', None)
        flange_type = getattr(row, 'flange_type', None)
        rating = getattr(row, 'rating', None)
        amount = _money(getattr(item, 'amount', None)) or Decimal('0')
        item_subtotal += amount
        if index == 0 and include_cost:
            raw_breakdown = getattr(item, 'cost_breakdown', None) or {}
            if isinstance(raw_breakdown, dict):
                costs = raw_breakdown.get('costs')
                if not isinstance(costs, dict):
                    costs = raw_breakdown
                keys = ('material', 'machining', 'surface', 'inspection',
                        'packaging', 'freight', 'management', 'total')
                cost_breakdown = {
                    key: _float(costs.get(key)) for key in keys if key in costs
                } or None
        size_label = getattr(row, 'size_label', '') or ''
        rating_label = getattr(rating, 'label', '') or ''
        description = (
            f'{getattr(standard, "short", "")} {rating_label} {size_label} '
            f'{getattr(flange_type, "name_zh", "")}'
        ).strip() or '法兰'
        items.append({
            'line_no': getattr(item, 'line_no', len(items) + 1),
            'description': description,
            'spec': getattr(standard, 'name', '') or '',
            'standard_edition': getattr(item, 'standard_edition', '') or getattr(standard, 'effective_edition', ''),
            'material': getattr(material, 'label', '') or getattr(material, 'code', '') or '—',
            'size_label': size_label,
            'rating_label': rating_label,
            'qty': getattr(item, 'qty', 0),
            'net_weight': _float(getattr(item, 'net_weight', None)),
            'weight_source': getattr(item, 'weight_source', '') or '',
            'weight_source_label': _weight_source_label(getattr(item, 'weight_source', '')),
            'weight_confidence': getattr(item, 'weight_confidence', '') or '',
            'unit_gross': _money(getattr(item, 'unit_price', None)),
            'unit_gross_display': _display_amount(getattr(item, 'unit_price', None), currency, fx_rate),
            'amount_net': amount,
            'amount_net_display': _display_amount(amount, currency, fx_rate),
            'lead_time_days': getattr(item, 'lead_time_days', None),
            'process_summary': _process_summary(getattr(item, 'process', None)),
            'snapshot_label': f'S-{getattr(quotation, "number", "")}',
        })
    total = _money(getattr(quotation, 'total', None))
    subtotal_value = _money(getattr(quotation, 'subtotal', None)) or item_subtotal
    tax_value = _money(getattr(quotation, 'tax_amount', None)) or Decimal('0')
    total_value = total if total is not None else subtotal_value + tax_value
    snapshot = getattr(quotation, 'snapshot', None)
    snapshot_label = f'#{getattr(snapshot, "pk", "")}' if snapshot is not None else '—'
    snapshot_params = getattr(snapshot, 'params', None) if snapshot is not None else None
    snapshot_meta = {}
    if isinstance(snapshot_params, dict):
        material_price_actual = _float(snapshot_params.get('material_price_per_kg'))
        material_price_base = _float(
            snapshot_params.get('material_price_base')
            or snapshot_params.get('material_price_base_per_kg')
        )
        machining_rate = _float(snapshot_params.get('machining_rate'))
        snapshot_meta = {
            'material_price': material_price_actual,
            'material_price_base': material_price_base,
            'material_price_source': snapshot_params.get('material_price_source') or '',
            'material_price_effective_from': snapshot_params.get('material_price_effective_from') or '',
            'quantity_discount_pct': _float(snapshot_params.get('quantity_discount_pct')),
            'demo_rates': bool(snapshot_params.get('demo_rates')),
            'machining_rate': machining_rate,
            'machining_rate_source': snapshot_params.get('machining_rate_source') or '',
            'rate_source': snapshot_params.get('rate_source') or '',
            'design_temp_c': snapshot_params.get('design_temp_c'),
            'design_pressure_bar': snapshot_params.get('design_pressure_bar'),
        }
    tax_pct = 13
    if isinstance(snapshot_params, dict) and snapshot_params.get('tax_pct') is not None:
        tax_pct = _float(snapshot_params.get('tax_pct')) or tax_pct
    created_by = getattr(quotation, 'created_by', None)
    customer = getattr(quotation, 'customer', None)
    status = getattr(quotation, 'status', '') or ''
    actions = _quote_actions(status)
    if user is not None:
        actions = [
            action for action in actions
            if permissions.can_transition(user, quotation, action['action'])
        ]
    return {
        'number': getattr(quotation, 'number', ''),
        'customer': getattr(customer, 'name', '') or getattr(customer, 'code', '') or '—',
        'customer_code': getattr(customer, 'code', '') or '',
        'customer_name': getattr(customer, 'name', '') or '',
        'project': '—',
        'status': status,
        'status_label': _status_label(status),
        'status_tone': _status_tone(status),
        'version': getattr(quotation, 'version', 1),
        'currency': currency,
        'symbol': symbol,
        'fx_rate': fx_rate,
        'incoterm': getattr(quotation, 'incoterm', '') or '—',
        'payment_terms': getattr(quotation, 'payment_terms', '') or '—',
        'quote_date': _date(getattr(quotation, 'created_at', None)),
        'valid_until': _date(getattr(quotation, 'valid_until', None)),
        'created_by': getattr(created_by, 'username', None) or getattr(created_by, 'get_username', lambda: '—')(),
        'notes': getattr(quotation, 'notes', '') or '',
        'items': items,
        'subtotal': subtotal_value,
        'subtotal_display': _display_amount(subtotal_value, currency, fx_rate),
        'tax_amount': tax_value,
        'tax_amount_display': _display_amount(tax_value, currency, fx_rate),
        'total': total_value,
        'total_display': _display_amount(total_value, currency, fx_rate),
        'tax_pct': tax_pct,
        'weight_rule': getattr(snapshot, 'weight_rule', 'w0.3') if snapshot is not None else 'w0.3',
        'snapshot_label': snapshot_label,
        'snapshot_meta': snapshot_meta,
        'demo': False,
        'cost_breakdown': cost_breakdown,
        'actions': actions,
    }


def _mock_quote(number, customer, status, version, quote_date, valid_until, qty, total, subtotal,
                tax_amount, incoterm='FOB', payment_terms='30% 预付，余款发货前', created_by='销售部'):
    return {
        'number': number,
        'customer': customer,
        'customer_code': '',
        'customer_name': customer,
        'project': '—',
        'symbol': '¥',
        'status': status,
        'status_label': _status_label(status),
        'status_tone': _status_tone(status),
        'version': version,
        'currency': 'CNY',
        'fx_rate': 7.10,
        'incoterm': incoterm,
        'payment_terms': payment_terms,
        'quote_date': quote_date,
        'valid_until': valid_until,
        'created_by': created_by,
        'notes': '示例样张：材料价、加工费、利润率与折扣为演示参数，不构成正式报价。',
        'items': [{
            'line_no': 1,
            'description': 'B16.5 Class 150 4″ 对焊法兰',
            'spec': 'ASME B16.5',
            'standard_edition': 'ASME B16.5-2025',
            'material': 'ASTM A105 / A105N',
            'size_label': '4″',
            'rating_label': 'Class 150',
            'qty': qty,
            'net_weight': 6.8,
            'weight_source': 'standard',
            'weight_source_label': '标准参考重量（B 级）',
            'weight_confidence': 'B',
            'unit_gross': (total / qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
            'unit_gross_display': (total / qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
            'amount_net': subtotal,
            'amount_net_display': subtotal,
            'lead_time_days': 8,
            'process_summary': '表面 不做处理 · 包装 木箱 · 检测 无',
            'snapshot_label': f'S-{number}',
        }],
        'subtotal': subtotal,
        'subtotal_display': subtotal,
        'tax_amount': tax_amount,
        'tax_amount_display': tax_amount,
        'total': total,
        'total_display': total,
        'tax_pct': 13,
        'weight_rule': 'w0.3',
        'snapshot_label': f'S-{number}',
        'demo': True,
        'cost_breakdown': dict(CONTRACT_FIXTURE['response']['costs']),
        'actions': [],
    }


MOCK_QUOTES = [
    _mock_quote(
        'FQ-2611-0001', 'ACME Piping', 'draft', 1,
        datetime.date(2026, 11, 6), datetime.date(2026, 11, 21), 10,
        Decimal('1705.50'), Decimal('1509.30'), Decimal('196.20'),
    ),
    _mock_quote(
        'FQ-2610-0012', '华东石化装备', 'sent', 2,
        datetime.date(2026, 10, 18), datetime.date(2026, 11, 2), 4,
        Decimal('682.20'), Decimal('603.72'), Decimal('78.48'),
        incoterm='EXW', payment_terms='电汇 30 天',
    ),
    _mock_quote(
        'FQ-2609-0031', 'Gulf Piping LLC', 'accepted', 3,
        datetime.date(2026, 9, 22), datetime.date(2026, 10, 7), 20,
        Decimal('3411.00'), Decimal('3018.60'), Decimal('392.40'),
        incoterm='CIF', payment_terms='信用证',
    ),
]
MOCK_QUOTES_BY_NUMBER = {quote['number']: quote for quote in MOCK_QUOTES}


def _build_sample_quote():
    sample = dict(MOCK_QUOTES[0])
    sample['items'] = [dict(item) for item in MOCK_QUOTES[0]['items']]
    sample['number'] = '预览样张 · 未保存'
    sample['status_label'] = '预览'
    sample['status_tone'] = 'info'
    sample['notes'] = '这是一个 A4 预览样张。点击「加入报价单」后，下方会生成你的报价单预览。'
    return sample


def _toggle_demo(request):
    return request.GET.get('demo') in ('1', 'true', 'yes')


@quote_login_required
def quote_list(request):
    status = (request.GET.get('status') or '').strip()
    keyword = (request.GET.get('q') or '').strip()
    demo = _toggle_demo(request)
    source_is_mock = False
    page_obj = None

    if not demo and _real_model_available():
        from django.db.models import Q

        try:
            queryset = _real_quotation_queryset(request.user).order_by('-created_at', '-number')
            if status:
                queryset = queryset.filter(status=status)
            if keyword:
                queryset = queryset.filter(
                    Q(number__icontains=keyword) | Q(customer__name__icontains=keyword)
                )
            paginator = Paginator(queryset, 20)
            page_obj = paginator.get_page(request.GET.get('page'))
            quotes = [
                _real_quote_to_dict(quote, user=request.user)
                for quote in page_obj.object_list
            ]
        except Exception as exc:  # pragma: no cover - T1 字段差异兜底
            logger.warning('读取报价单列表失败，回退样张：%s', exc)
            source_is_mock = True
            quotes = list(MOCK_QUOTES)
    else:
        quotes = list(MOCK_QUOTES)
        source_is_mock = True
        if status:
            quotes = [quote for quote in quotes if quote['status'] == status]
        if keyword:
            needle = keyword.lower()
            quotes = [
                quote for quote in quotes
                if needle in quote['number'].lower() or needle in quote['customer'].lower()
            ]
        paginator = Paginator(quotes, 20)
        page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'page': 'list',
        'quotes': quotes,
        'page_obj': page_obj,
        'is_mock': source_is_mock,
        'status': status,
        'keyword': keyword,
        'status_choices': [
            ('draft', '草稿'), ('pending', '待审批'), ('approved', '已批准'),
            ('sent', '已发送'), ('accepted', '已接受'), ('rejected', '已拒绝'),
            ('expired', '已过期'), ('ordered', '已转订单'), ('cancelled', '已作废'),
        ],
        'status_labels': STATUS_LABELS,
        'scope_label': permissions.quote_scope_label(request.user),
        'role_label': _role_label(request.user),
        'can_create_quote': permissions.can_create_quote(request.user),
    }
    return render(request, 'quote/list.html', context)


@quote_login_required
def quote_detail(request, number):
    demo = _toggle_demo(request)
    # 详情成本只对明确成本角色可见；兼容模式默认不展示，避免销售误看
    can_view_cost = permissions.role_for(request.user) in permissions.COST_ROLES
    if demo:
        quote = MOCK_QUOTES_BY_NUMBER.get(number)
        if quote is None:
            raise Http404('报价单不存在')
    elif _real_model_available():
        quotation = _find_quotation_model(number)
        if quotation is None:
            raise Http404('报价单不存在')
        if not permissions.can_view_quote(request.user, quotation):
            return _forbidden(request, '当前角色不能查看该报价单（销售仅可查看本人创建的报价）。')
        quote = _real_quote_to_dict(quotation, user=request.user)
        if not can_view_cost:
            quote['snapshot_meta'] = {}
            quote['cost_breakdown'] = None
    else:
        quote = MOCK_QUOTES_BY_NUMBER.get(number)
        if quote is None:
            raise Http404('报价单不存在')
    return render(request, 'quote/detail.html', {
        'page': 'detail',
        'quote': quote,
        'upper': rmb_upper(quote['total']) if quote['currency'] != 'USD' else '—（USD 报价不转人民币大写）',
        'weight_source_label': _weight_source_label,
        'can_view_cost': can_view_cost,
        'role_label': _role_label(request.user),
    })


def quote_print(request, number):
    """A4 打印页。

    普通访问需要登录；PDF 渲染进程通过 `print_page_token` 短时效签名令牌访问，
    避免为了出 PDF 把页面改成公开（见 quote/pdf.py）。
    """
    demo = _toggle_demo(request)
    token_ok = verify_print_page_token(request.GET.get('pdf_token'), number, demo=demo)
    if not token_ok and not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
    if demo:
        quote = MOCK_QUOTES_BY_NUMBER.get(number)
    elif _real_model_available():
        quotation = _find_quotation_model(number)
        if quotation is None:
            raise Http404('报价单不存在')
        if not token_ok and not permissions.can_view_quote(request.user, quotation):
            return _forbidden(request, '当前角色不能打印该报价单（销售仅可打印本人创建的报价）。')
        quote = _real_quote_to_dict(quotation, user=request.user if not token_ok else None)
    else:
        quote = MOCK_QUOTES_BY_NUMBER.get(number)
    if quote is None:
        raise Http404('报价单不存在')
    return render(request, 'quote/print.html', {
        'quote': quote,
        'upper': rmb_upper(quote['total']) if quote['currency'] != 'USD' else '—（USD 报价不转人民币大写）',
        'weight_source_label': _weight_source_label,
        'pdf_url': reverse('quote:pdf', args=[number]) + ('?demo=1' if demo else ''),
        'pdf_token': print_page_token(number, demo=demo),
    })


@quote_login_required
def quote_pdf(request, number):
    demo = _toggle_demo(request)
    if demo:
        quote = MOCK_QUOTES_BY_NUMBER.get(number)
    elif _real_model_available():
        quotation = _find_quotation_model(number)
        if quotation is None:
            raise Http404('报价单不存在')
        if not permissions.can_view_quote(request.user, quotation):
            return _forbidden(request, '当前角色不能下载该报价单 PDF（销售仅可下载本人创建的报价）。')
        quote = _real_quote_to_dict(quotation, user=request.user)
    else:
        quote = MOCK_QUOTES_BY_NUMBER.get(number)
    if quote is None:
        raise Http404('报价单不存在')
    try:
        path = render_quote_pdf(number, demo=demo, force=request.GET.get('refresh') == '1')
    except Exception as exc:  # RuntimeError（Playwright 缺失）或渲染失败
        logger.warning('报价单 PDF 生成失败 %s：%s', number, exc)
        return render(request, 'quote/pdf_unavailable.html', {
            'page': 'pdf',
            'quote': quote,
            'message': str(exc),
            'print_url': reverse('quote:print', args=[number]) + ('?demo=1' if demo else ''),
            'back_url': reverse('quote:detail', args=[number]) + ('?demo=1' if demo else ''),
        }, status=503)
    response = FileResponse(open(path, 'rb'), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{quote["number"].replace(" ", "-")}.pdf"'
    return response


# ---------------------------------------------------------------------------
# Excel 导出视图
# ---------------------------------------------------------------------------

@quote_login_required
def quote_excel(request, number):
    """详情导出单张报价单 .xlsx；权限与详情页一致。"""
    demo = _toggle_demo(request)
    include_cost = _quote_include_cost(request.user)
    if demo:
        mock_quote = MOCK_QUOTES_BY_NUMBER.get(number)
        quote = dict(mock_quote) if mock_quote else None
    elif _real_model_available():
        quotation = _find_quotation_model(number)
        if quotation is None:
            raise Http404('报价单不存在')
        if not permissions.can_view_quote(request.user, quotation):
            return _forbidden(request, '当前角色不能导出该报价单 Excel（销售仅可导出本人创建的报价）。')
        quote = _real_quote_to_dict(quotation, user=request.user)
    else:
        mock_quote = MOCK_QUOTES_BY_NUMBER.get(number)
        quote = dict(mock_quote) if mock_quote else None
    if quote is None:
        raise Http404('报价单不存在')
    _prepare_export_quote(quote, include_cost)
    content = build_quote_workbook(
        quote,
        include_cost=include_cost,
        generated_at=_export_generated_at(),
    )
    return _xlsx_response(content, f"{quote.get('number') or 'quote'}.xlsx")


@quote_login_required
def quote_export(request):
    """列表导出当前筛选：汇总页 + 每张报价单 sheet。"""
    status = (request.GET.get('status') or '').strip()
    keyword = (request.GET.get('q') or '').strip()
    demo = _toggle_demo(request)
    include_cost = _quote_include_cost(request.user)
    quotes = []

    if not demo and _real_model_available():
        from django.db.models import Q

        try:
            queryset = _real_quotation_queryset(request.user).order_by('-created_at', '-number')
            if status:
                queryset = queryset.filter(status=status)
            if keyword:
                queryset = queryset.filter(
                    Q(number__icontains=keyword) | Q(customer__name__icontains=keyword)
                )
            quotes = [_real_quote_to_dict(quote, user=request.user) for quote in queryset]
        except Exception as exc:  # pragma: no cover - 字段差异兜底
            logger.warning('导出报价单列表失败，回退样张：%s', exc)
            quotes = [dict(quote) for quote in MOCK_QUOTES if not status or quote['status'] == status]
    else:
        quotes = [dict(quote) for quote in MOCK_QUOTES if not status or quote['status'] == status]
        if keyword:
            needle = keyword.lower()
            quotes = [
                quote for quote in quotes
                if needle in quote['number'].lower() or needle in quote['customer'].lower()
            ]

    for quote in quotes:
        _prepare_export_quote(quote, include_cost)

    filters = []
    if status:
        filters.append(f'状态={STATUS_LABELS.get(status, status)}')
    if keyword:
        filters.append(f'关键字={keyword}')
    filter_text = ' · '.join(filters) if filters else '全部'
    content = build_quotes_workbook(
        quotes,
        include_cost=include_cost,
        generated_at=_export_generated_at(),
        filter_text=filter_text,
    )
    today = datetime.date.today().strftime('%Y%m%d')
    return _xlsx_response(content, f'报价单导出-{today}.xlsx')


# ---------------------------------------------------------------------------
# 客户（只读；客户维护与折扣由 T3 / Django Admin 负责）
# ---------------------------------------------------------------------------

@quote_login_required
def customers(request):
    rows = _db_customers()
    return render(request, 'quote/customers.html', {
        'page': 'customers',
        'customers': rows,
        'has_customers': bool(rows),
    })


# ---------------------------------------------------------------------------
# 价格库（只读，读 T3 数据）
# ---------------------------------------------------------------------------

def _price_library_context():
    """从 T3 价格库读取材料价 / 工艺费率 / 客户；不再回退静态占位。"""
    materials = []
    rates = []
    customers = _db_customers()
    db_error = ''
    try:
        from .models import Material, MaterialPrice, ProcessRate

        for material in Material.objects.filter(active=True).order_by('sort_order', 'code'):
            latest = (
                MaterialPrice.objects.filter(material=material)
                .order_by('-effective_from', '-id')
                .first()
            )
            source = getattr(latest, 'source', '') if latest else ''
            materials.append({
                'code': material.code,
                'label': material.label,
                'family': material.family,
                'density': _float(material.density),
                'price': _float(latest.price_per_kg) if latest else None,
                'currency': getattr(latest, 'currency', 'CNY') if latest else 'CNY',
                'effective_from': latest.effective_from if latest else None,
                'source': source,
                'source_label': SOURCE_LABELS.get(source, source or '—'),
                'note': getattr(latest, 'note', '') if latest else '',
                'is_demo': source == 'demo',
            })
        for rate in ProcessRate.objects.order_by('code'):
            source = getattr(rate, 'source', '')
            tiers = rate.tiers or {}
            tiers_text = '、'.join(f'{key}: {value}' for key, value in tiers.items()) if isinstance(tiers, dict) else ''
            rates.append({
                'code': rate.code,
                'label': rate.label,
                'unit_label': rate.get_unit_display(),
                'rate': _float(rate.rate),
                'tiers_text': tiers_text,
                'effective_from': rate.effective_from,
                'source': source,
                'source_label': SOURCE_LABELS.get(source, source or '—'),
                'is_demo': source == 'demo',
            })
    except Exception as exc:  # pragma: no cover - 迁移未执行时的兜底
        db_error = str(exc)
        logger.debug('读取价格库失败', exc_info=True)

    has_demo = any(item['is_demo'] for item in materials + rates) or any(
        customer.get('is_demo') for customer in customers
    )
    return {
        'materials': materials,
        'rates': rates,
        'customers': customers,
        'has_demo': has_demo,
        'has_data': bool(materials or rates or customers),
        'db_error': db_error,
    }


@quote_login_required
def prices(request):
    if not permissions.can_view_cost(request.user):
        return _forbidden(request, '价格库包含成本数据，仅技术 / 财务 / 经理 / 管理员角色可见。')
    context = _price_library_context()
    context.update({'page': 'prices', 'default_params': DEFAULT_PARAMS})
    return render(request, 'quote/prices.html', context)
