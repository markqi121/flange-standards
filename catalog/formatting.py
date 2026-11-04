"""显示格式与数据换算的小工具。"""

from decimal import Decimal


def fmt_dim(value):
    """尺寸显示：整数不带小数点，非整数保留标准给的小数位（最多 2 位）。

    数据包统一按 0.1 mm 转录，个别标准给到 0.01。这里不补零、不四舍五入，
    避免把 46 显示成 46.0，也避免把 60.5 显示成 61。
    """
    if value is None or value == '':
        return ''
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    text = f'{number:.2f}'.rstrip('0').rstrip('.')
    return text


def fmt_weight(value):
    if value is None or value == '':
        return ''
    return fmt_dim(value)


def fmt_mm(value):
    text = fmt_dim(value)
    return f'{text} mm' if text else ''


def normalize_decimal(text):
    """把欧洲写法的小数逗号 16,0 转成 16.0；非数字原样返回。"""
    if not isinstance(text, str):
        return text
    candidate = text.strip().replace(' ', '')
    if ',' in candidate and '.' not in candidate:
        candidate = candidate.replace(',', '.')
    try:
        float(candidate)
    except ValueError:
        return text
    return candidate


def temp_label(raw):
    """把标准里带脚注的温度写法压成简短标签。"""
    if not isinstance(raw, str):
        return str(raw)
    text = raw.strip()
    for junk in (' [Note (6)]', ' [Note 6]', ' [Note (1)]', ' [Note (2)]',
                 ' [Note (3)]', ' [Note (4)]', ' [Note (5)]'):
        text = text.replace(junk, '')
    lower = text.lower()
    if lower.startswith('min. to'):
        text = '≤ ' + text.split('to', 1)[1].strip()
    elif lower.startswith('t_l to'):
        text = '≤ ' + text.split('to', 1)[1].strip()
    return text


def parse_float(value):
    try:
        return float(Decimal(str(value).replace(',', '.')))
    except Exception:
        return None
