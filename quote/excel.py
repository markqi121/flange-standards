"""法兰报价 · Excel（.xlsx）导出（T11）。

用 openpyxl 生成可打印的报价单工作簿：
- 单张报价单：标题/编号/版本/客户/币种汇率/有效期/贸易条款 + 明细 + 小计/税/合计；
- 列表导出：汇总页 + 每张报价单一个 sheet；
- 成本区块只在 include_cost=True（授权角色）时输出；
- 演示数据显式警示；设置 A4 横向、打印区域、缩放一页宽。

不实现任何成本/定价算法：金额全部来自 quote/services 计算后的快照/明细。
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

MONEY_FORMAT = '#,##0.00'
WEIGHT_FORMAT = '0.000'
MAX_COL = 8

TITLE_FONT = Font(size=18, bold=True, color='FFFFFF')
SUBTITLE_FONT = Font(size=9, color='56697D')
LABEL_FONT = Font(size=9, bold=True, color='56697D')
VALUE_FONT = Font(size=10, color='0B1B2B')
HEADER_FONT = Font(size=9, bold=True, color='FFFFFF')
SECTION_FONT = Font(size=10, bold=True, color='0B1B2B')
SMALL_FONT = Font(size=8, color='56697D')
WARN_FONT = Font(size=9, bold=True, color='8F2218')
BOLD_FONT = Font(size=10, bold=True, color='0B1B2B')

TITLE_FILL = PatternFill('solid', fgColor='0B1B2B')
HEADER_FILL = PatternFill('solid', fgColor='0B1B2B')
SECTION_FILL = PatternFill('solid', fgColor='E8EEF6')
WARN_FILL = PatternFill('solid', fgColor='FDE9E0')
TOTAL_FILL = PatternFill('solid', fgColor='F8FAFC')
THIN_SIDE = Side(style='thin', color='C2CFDD')
BORDER = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)
CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
LEFT = Alignment(horizontal='left', vertical='center', wrap_text=True)
RIGHT = Alignment(horizontal='right', vertical='center')
WARN_ALIGN = Alignment(horizontal='left', vertical='center', wrap_text=True)

COLUMN_WIDTHS = {
    'A': 12, 'B': 42, 'C': 24, 'D': 8, 'E': 13, 'F': 16, 'G': 18, 'H': 11,
}


def _text(value, default='—'):
    if value is None or value == '':
        return default
    return str(value)


def _num(value):
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_text(value):
    if value is None or value == '':
        return '—'
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def _safe_sheet_title(title, used_titles):
    cleaned = ''.join('_' if ch in '[]:*?/\\' else ch for ch in str(title or '报价单'))
    cleaned = cleaned.strip() or '报价单'
    cleaned = cleaned[:31]
    candidate = cleaned
    index = 2
    while candidate in used_titles:
        suffix = f'-{index}'
        candidate = cleaned[:31 - len(suffix)] + suffix
        index += 1
    used_titles.add(candidate)
    return candidate


def _apply_column_widths(ws):
    for column, width in COLUMN_WIDTHS.items():
        ws.column_dimensions[column].width = width


def _apply_sheet_layout(ws, last_row, last_col=MAX_COL, landscape=True):
    ws.sheet_view.showGridLines = False
    ws.print_area = f'A1:{get_column_letter(last_col)}{last_row}'
    ws.page_setup.orientation = 'landscape' if landscape else 'portrait'
    ws.page_setup.paperSize = '9'  # A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_margins.left = 0.35
    ws.page_margins.right = 0.35
    ws.page_margins.top = 0.5
    ws.page_margins.bottom = 0.5
    ws.page_margins.header = 0.2
    ws.page_margins.footer = 0.2


def _merge_write(ws, row, col_start, col_end, value, font=None, alignment=None, fill=None):
    if col_end > col_start:
        ws.merge_cells(start_row=row, start_column=col_start, end_row=row, end_column=col_end)
    cell = ws.cell(row=row, column=col_start, value=value)
    if font is not None:
        cell.font = font
    if alignment is not None:
        cell.alignment = alignment
    if fill is not None:
        cell.fill = fill
    return cell


def _style_border(ws, row_start, row_end, col_start, col_end):
    for row in ws.iter_rows(min_row=row_start, max_row=row_end, min_col=col_start, max_col=col_end):
        for cell in row:
            cell.border = BORDER


def _quote_title(quote):
    return '报 价 单'


def _currency_label(quote):
    currency = _text(quote.get('currency'), 'CNY')
    symbol = quote.get('symbol') or ('US$' if currency == 'USD' else '¥')
    if currency == 'USD':
        return f'{currency} / {symbol} · 1 USD = {_num(quote.get("fx_rate")) or 0:.2f} CNY'
    return f'{currency} / {symbol} · 基准币种'


def _item_unit(item):
    value = item.get('unit_gross_display')
    return value if value is not None else item.get('unit_gross')


def _item_amount(item):
    value = item.get('amount_net_display')
    return value if value is not None else item.get('amount_net')


def _add_meta_row(ws, row, left_label, left_value, right_label=None, right_value=None, third_label=None, third_value=None):
    _merge_write(ws, row, 1, 1, left_label, LABEL_FONT, LEFT)
    _merge_write(ws, row, 2, 3, left_value, VALUE_FONT, LEFT)
    if right_label is not None:
        _merge_write(ws, row, 4, 4, right_label, LABEL_FONT, LEFT)
        _merge_write(ws, row, 5, 5, right_value, VALUE_FONT, LEFT)
    if third_label is not None:
        _merge_write(ws, row, 6, 6, third_label, LABEL_FONT, LEFT)
        _merge_write(ws, row, 7, 8, third_value, VALUE_FONT, LEFT)


def _material_source_text(meta):
    source = (meta or {}).get('material_price_source') or ''
    if source == 'manual':
        return '本次覆盖'
    if source:
        return f'价格库（{source}）'
    return '价格库'


def _machining_source_text(meta):
    source = (meta or {}).get('machining_rate_source') or ''
    return {
        'manual': '本次覆盖',
        'process': '价格库 / 档位',
        'default': '默认档位',
    }.get(source, '—')


def build_quote_sheet(wb, quote, include_cost=False, generated_at='', sheet_title=None, used_titles=None):
    """在 workbook 中追加一张报价单 sheet，返回 worksheet。"""
    used_titles = used_titles if used_titles is not None else set()
    if sheet_title is None:
        sheet_title = _safe_sheet_title(quote.get('number') or '报价单', used_titles)
    ws = wb.create_sheet(title=sheet_title)
    _apply_column_widths(ws)
    last_col = MAX_COL
    symbol = quote.get('symbol') or ('US$' if quote.get('currency') == 'USD' else '¥')
    currency = _text(quote.get('currency'), 'CNY')

    # 标题
    _merge_write(ws, 1, 1, last_col, _quote_title(quote), TITLE_FONT, CENTER, TITLE_FILL)
    ws.row_dimensions[1].height = 30
    subtitle = f'{_text(quote.get("customer"))} · {_date_text(quote.get("quote_date"))} · {currency}'
    if quote.get('demo'):
        subtitle += ' · 演示样张'
    _merge_write(ws, 2, 1, last_col, subtitle, SUBTITLE_FONT, LEFT)

    # 基础信息
    _add_meta_row(
        ws, 3,
        '报价单号', _text(quote.get('number')),
        '版本', f"V{_text(quote.get('version'), '1')}",
        '状态', _text(quote.get('status_label')),
    )
    _add_meta_row(
        ws, 4,
        '客户', _text(quote.get('customer')),
        '报价日期', _date_text(quote.get('quote_date')),
        '有效期至', _date_text(quote.get('valid_until')),
    )
    _add_meta_row(
        ws, 5,
        '币种 / 汇率', _currency_label(quote),
        '贸易条款', _text(quote.get('incoterm')),
        '付款条件', _text(quote.get('payment_terms')),
    )
    _add_meta_row(
        ws, 6,
        '报价人', _text(quote.get('created_by')),
        '项目 / 询价号', _text(quote.get('project')),
        '导出时间', generated_at or '',
    )

    # 明细表头
    header_row = 8
    headers = [
        '#', '产品描述', '材质', '数量', f'单重 (kg)',
        f'含税单价（{symbol}）', f'金额（不含税，{symbol}）', '交期 (天)',
    ]
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = CENTER
        cell.border = BORDER
    ws.row_dimensions[header_row].height = 26

    row = header_row + 1
    items = quote.get('items') or []
    for index, item in enumerate(items, start=1):
        ws.cell(row=row, column=1, value=index).alignment = CENTER
        description = _text(item.get('description'))
        extra = []
        if item.get('standard_edition'):
            extra.append(str(item['standard_edition']))
        if item.get('weight_source_label'):
            extra.append(str(item['weight_source_label']))
        if item.get('snapshot_label'):
            extra.append(f"快照 {item['snapshot_label']}")
        if extra:
            description = f'{description}\n{" · ".join(extra)}'
        ws.cell(row=row, column=2, value=description).alignment = LEFT
        ws.cell(row=row, column=3, value=_text(item.get('material'))).alignment = LEFT
        ws.cell(row=row, column=4, value=item.get('qty')).alignment = CENTER
        net_cell = ws.cell(row=row, column=5, value=_num(item.get('net_weight')))
        net_cell.number_format = WEIGHT_FORMAT
        unit_cell = ws.cell(row=row, column=6, value=_num(_item_unit(item)))
        unit_cell.number_format = MONEY_FORMAT
        amount_cell = ws.cell(row=row, column=7, value=_num(_item_amount(item)))
        amount_cell.number_format = MONEY_FORMAT
        ws.cell(row=row, column=8, value=item.get('lead_time_days')).alignment = CENTER
        _style_border(ws, row, row, 1, last_col)
        row += 1

    if not items:
        _merge_write(ws, row, 1, last_col, '暂无报价明细', SMALL_FONT, CENTER)
        _style_border(ws, row, row, 1, last_col)
        row += 1

    # 金额合计
    totals = [
        ('不含税小计', quote.get('subtotal_display', quote.get('subtotal')), False),
        (f"税额（{_num(quote.get('tax_pct')) or 0:.0f}%）", quote.get('tax_amount_display', quote.get('tax_amount')), False),
        ('含税合计', quote.get('total_display', quote.get('total')), True),
    ]
    for label, value, strong in totals:
        _merge_write(ws, row, 1, 6, label, BOLD_FONT if strong else VALUE_FONT, RIGHT, TOTAL_FILL)
        cell = ws.cell(row=row, column=7, value=_num(value))
        cell.number_format = MONEY_FORMAT
        cell.font = BOLD_FONT if strong else VALUE_FONT
        cell.fill = TOTAL_FILL
        cell.alignment = RIGHT
        ws.cell(row=row, column=8, value='').fill = TOTAL_FILL
        _style_border(ws, row, row, 1, last_col)
        row += 1

    upper = quote.get('upper') or ''
    if currency == 'CNY' and upper:
        _merge_write(ws, row, 1, 6, '合计大写', VALUE_FONT, RIGHT)
        _merge_write(ws, row, 7, 8, upper, VALUE_FONT, LEFT)
        _style_border(ws, row, row, 1, last_col)
        row += 1

    # 成本区块（仅授权角色）
    cost = quote.get('cost_breakdown') or {}
    if include_cost and cost:
        row += 1
        _merge_write(ws, row, 1, last_col, '单件成本明细（CNY，仅授权角色可见）', SECTION_FONT, LEFT, SECTION_FILL)
        _style_border(ws, row, row, 1, last_col)
        row += 1
        cost_rows = [
            ('材料', 'material'), ('加工', 'machining'),
            ('表面处理', 'surface'), ('检测', 'inspection'),
            ('包装', 'packaging'), ('运费', 'freight'),
            ('管理费', 'management'), ('单件成本合计', 'total'),
        ]
        for label, key in cost_rows:
            strong = key == 'total'
            ws.cell(row=row, column=1, value=label).font = BOLD_FONT if strong else VALUE_FONT
            ws.cell(row=row, column=1).alignment = LEFT
            _merge_write(ws, row, 2, 3, '', VALUE_FONT, LEFT)
            value_cell = ws.cell(row=row, column=4, value=_num(cost.get(key)))
            value_cell.number_format = MONEY_FORMAT
            value_cell.alignment = RIGHT
            value_cell.font = BOLD_FONT if strong else VALUE_FONT
            _style_border(ws, row, row, 1, 4)
            row += 1

    # 动态价格参数（本次 / 基准，仅授权角色）
    snapshot_meta = quote.get('snapshot_meta') or {}
    if include_cost and snapshot_meta and (
        snapshot_meta.get('material_price') is not None
        or snapshot_meta.get('machining_rate') is not None
    ):
        row += 1
        _merge_write(ws, row, 1, last_col, '动态价格参数（本次 / 基准，仅授权角色可见）', SECTION_FONT, LEFT, SECTION_FILL)
        _style_border(ws, row, row, 1, last_col)
        row += 1
        material_actual = snapshot_meta.get('material_price')
        material_base = snapshot_meta.get('material_price_base')
        material_text = (
            f"¥{_num(material_actual):.2f} / kg / 基准 ¥{_num(material_base):.2f} / kg"
            if material_actual is not None and material_base is not None
            else (f"¥{_num(material_actual):.2f} / kg" if material_actual is not None else '—')
        )
        dynamic_rows = [
            ('材料价（本次 / 基准）', material_text),
            ('材料价来源', _material_source_text(snapshot_meta) +
             (f" · 生效 {snapshot_meta.get('material_price_effective_from')}"
              if snapshot_meta.get('material_price_effective_from') else '')),
            ('工资率（本次）',
             f"¥{_num(snapshot_meta.get('machining_rate')):.2f} 元/件"
             if snapshot_meta.get('machining_rate') is not None else '按价格库 / 档位'),
            ('工资率来源', _machining_source_text(snapshot_meta)),
        ]
        for label, value in dynamic_rows:
            ws.cell(row=row, column=1, value=label).font = VALUE_FONT
            ws.cell(row=row, column=1).alignment = LEFT
            _merge_write(ws, row, 2, last_col, value, SMALL_FONT, LEFT)
            _style_border(ws, row, row, 1, last_col)
            row += 1

    # 备注 / 数据来源 / 演示警示
    row += 1
    notes = quote.get('notes') or ''
    if notes:
        _merge_write(ws, row, 1, 2, '备注', LABEL_FONT, LEFT)
        _merge_write(ws, row, 3, last_col, str(notes), SMALL_FONT, WARN_ALIGN)
        row += 1

    demo_warning = bool(quote.get('demo')) or bool((quote.get('snapshot_meta') or {}).get('demo_rates'))
    if demo_warning:
        _merge_write(
            ws, row, 1, last_col,
            '⚠ 演示数据警示：本报价单含演示材料价 / 费率 / 示例参数，仅用于联调、UAT 与测试，禁止作为正式报价或合同依据。',
            WARN_FONT, WARN_ALIGN, WARN_FILL,
        )
        ws.row_dimensions[row].height = 26
        row += 1

    _merge_write(
        ws, row, 1, last_col,
        '数据来源：法兰标准库（ASME B16.5 / B16.47 / EN 1092-1 / JIS B2220，事实性表格整理）· '
        f"成本快照 {_text(quote.get('snapshot_label'))} · 重量规则 {_text(quote.get('weight_rule'), 'w0.3')} · "
        '金额四舍五入到 2 位。',
        SMALL_FONT, LEFT,
    )
    row += 1
    _merge_write(ws, row, 1, last_col, '打印：A4 · 横向 · 一页宽 · 第 1 页 / 共 1 页', SMALL_FONT, LEFT)
    last_row = row

    _apply_sheet_layout(ws, last_row, last_col=last_col, landscape=True)
    return ws


def build_quote_workbook(quote, include_cost=False, generated_at=''):
    """单张报价单工作簿，返回 xlsx bytes。"""
    wb = Workbook()
    default = wb.active
    wb.remove(default)
    build_quote_sheet(wb, quote, include_cost=include_cost, generated_at=generated_at)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_quotes_workbook(quotes, include_cost=False, generated_at='', filter_text=''):
    """列表导出：汇总页 + 每张报价单 sheet，返回 xlsx bytes。"""
    wb = Workbook()
    summary = wb.active
    summary.title = '报价单列表'
    _apply_column_widths(summary)
    last_col = 10
    _merge_write(summary, 1, 1, last_col, '报 价 单 列 表', TITLE_FONT, CENTER, TITLE_FILL)
    summary.row_dimensions[1].height = 30
    subtitle = f'导出时间：{generated_at or "—"} · 共 {len(quotes)} 张'
    if filter_text:
        subtitle += f' · 筛选：{filter_text}'
    _merge_write(summary, 2, 1, last_col, subtitle, SUBTITLE_FONT, LEFT)

    headers = ['#', '单号', '客户', '状态', '版本', '币种', '含税合计', '报价日期', '有效期至', '明细行']
    header_row = 4
    for col, header in enumerate(headers, start=1):
        cell = summary.cell(row=header_row, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = CENTER
        cell.border = BORDER
    summary.column_dimensions['B'].width = 18
    summary.column_dimensions['C'].width = 28
    summary.column_dimensions['G'].width = 16
    summary.column_dimensions['H'].width = 12
    summary.column_dimensions['I'].width = 12

    row = header_row + 1
    sum_total = 0.0
    for index, quote in enumerate(quotes, start=1):
        summary.cell(row=row, column=1, value=index).alignment = CENTER
        summary.cell(row=row, column=2, value=_text(quote.get('number'))).alignment = LEFT
        summary.cell(row=row, column=3, value=_text(quote.get('customer'))).alignment = LEFT
        summary.cell(row=row, column=4, value=_text(quote.get('status_label'))).alignment = CENTER
        summary.cell(row=row, column=5, value=f"V{_text(quote.get('version'), '1')}").alignment = CENTER
        summary.cell(row=row, column=6, value=_text(quote.get('currency'), 'CNY')).alignment = CENTER
        total_value = _num(quote.get('total_display', quote.get('total')))
        summary.cell(row=row, column=7, value=total_value).number_format = MONEY_FORMAT
        if total_value:
            sum_total += total_value
        summary.cell(row=row, column=8, value=_date_text(quote.get('quote_date'))).alignment = CENTER
        summary.cell(row=row, column=9, value=_date_text(quote.get('valid_until'))).alignment = CENTER
        summary.cell(row=row, column=10, value=len(quote.get('items') or [])).alignment = CENTER
        _style_border(summary, row, row, 1, last_col)
        row += 1

    if not quotes:
        _merge_write(summary, row, 1, last_col, '没有符合条件的报价单', SMALL_FONT, CENTER)
        row += 1

    _merge_write(summary, row, 1, 6, '合计（含税）', BOLD_FONT, RIGHT, TOTAL_FILL)
    total_cell = summary.cell(row=row, column=7, value=round(sum_total, 2))
    total_cell.number_format = MONEY_FORMAT
    total_cell.font = BOLD_FONT
    total_cell.fill = TOTAL_FILL
    total_cell.alignment = RIGHT
    _style_border(summary, row, row, 1, last_col)
    last_row = row
    _apply_sheet_layout(summary, last_row, last_col=last_col, landscape=True)

    used_titles = {summary.title}
    for quote in quotes:
        build_quote_sheet(
            wb, quote, include_cost=include_cost, generated_at=generated_at,
            used_titles=used_titles,
        )

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
