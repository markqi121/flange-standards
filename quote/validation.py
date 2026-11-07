"""报价校验：压力–温度（p/T）、组合、数量、折扣、价格有效期。

p/T 数据
--------
本模块内置 ASME B16.5-2025 的 SI（bar）额定值表，只覆盖前端原型已接入的
材料组 **1.1 / 2.1 / 2.3**（契约 §3.2 示例、方案原型一致）：

- 组 1.1：碳钢 / 碳锰钢 / 低镍钢（A105、A350 LF2…），38–538 °C；
- 组 2.1：304 系奥氏体不锈钢（F304…），38–816 °C；
- 组 2.3：316L/304L 系（F316L…），38–454 °C。

插值规则（ASME B16.5 para. 2.4）：
- 设计温度 ≤ 表内最低温度：取最低温度额定值（floor，不向下外推）；
- 两个列表温度之间：线性插值；
- 高于表内最高温度 / 该温度无额定值：不通过，不得外推；
- 压力等级之间不插值（每级是独立曲线）。

未接入的材料组返回 ``na`` 并显式提示，不静默当作通过（P1 再补 13 组）。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional

from django.utils import timezone

PT_SOURCE = 'ASME B16.5-2025 Tables 2-1.1 / 2-2.1 / 2-2.3 (SI, bar)'

# 材料组 -> (温度列表, {Class: 额定压力 bar})；None 表示该温度无额定值
PT_TABLES: Dict[str, dict] = {
    '1.1': {
        'temps': [38, 50, 100, 150, 200, 250, 300, 325, 350, 375, 400, 425, 450, 475, 500, 538],
        'classes': {
            '150': [19.6, 19.1, 17.7, 15.8, 13.8, 12.1, 10.2, 9.3, 8.4, 7.4, 6.5, 5.5, 4.6, 3.7, 2.8, 1.4],
            '300': [51.0, 49.8, 46.7, 45.1, 43.8, 42.0, 39.9, 38.7, 37.6, 36.4, 34.6, 28.7, 23.0, 17.5, 11.5, 5.8],
            '400': [68.0, 66.4, 62.3, 60.1, 58.4, 56.0, 53.2, 51.6, 50.2, 48.5, 46.2, 38.3, 30.6, 23.3, 15.4, 7.7],
            '600': [102.0, 99.6, 93.4, 90.1, 87.6, 83.9, 79.8, 77.3, 75.3, 72.8, 69.3, 57.5, 45.9, 35.0, 23.0, 11.6],
            '900': [153.1, 149.3, 140.1, 135.2, 131.5, 125.9, 119.7, 116.0, 112.9, 109.2, 103.9, 86.2, 68.9, 52.5, 34.6, 17.4],
            '1500': [255.1, 248.9, 233.5, 225.3, 219.1, 209.8, 199.5, 193.4, 188.2, 182.1, 173.1, 143.7, 114.9, 87.4, 57.6, 29.0],
            '2500': [425.1, 414.9, 389.1, 375.4, 365.1, 349.7, 332.6, 322.3, 313.7, 303.4, 288.6, 239.4, 191.4, 145.7, 96.0, 48.4],
        },
    },
    '2.1': {
        'temps': [38, 50, 100, 150, 200, 250, 300, 325, 350, 375, 400, 425, 450, 475, 500, 538,
                  550, 575, 600, 625, 650, 675, 700, 725, 750, 775, 800, 816],
        'classes': {
            '150': [19.1, 18.5, 15.6, 14.2, 13.1, 12.1, 10.2, 9.3, 8.4, 7.4, 6.5, 5.5, 4.6, 3.7, 2.8, 1.4,
                    None, None, None, None, None, None, None, None, None, None, None, None],
            '300': [49.7, 48.3, 40.8, 37.0, 34.3, 32.2, 31.0, 30.2, 29.5, 29.0, 28.3, 28.1, 27.4, 26.9, 26.4, 24.4,
                    24.0, 21.0, 16.8, 13.7, 11.2, 9.3, 8.0, 6.7, 5.7, 4.8, 3.8, 3.3],
            '400': [66.3, 64.5, 54.4, 49.4, 45.7, 42.9, 41.3, 40.3, 39.4, 38.7, 37.8, 37.4, 36.5, 35.8, 35.2, 32.5,
                    32.0, 28.0, 22.4, 18.3, 15.0, 12.4, 10.7, 9.0, 7.6, 6.4, 5.1, 4.4],
            '600': [99.4, 96.7, 81.6, 74.1, 68.6, 64.3, 61.9, 60.5, 59.0, 58.1, 56.6, 56.2, 54.7, 53.8, 52.8, 48.8,
                    48.0, 42.0, 33.6, 27.5, 22.5, 18.7, 16.0, 13.4, 11.5, 9.5, 7.6, 6.6],
            '900': [149.1, 145.0, 122.4, 111.1, 102.9, 96.5, 92.9, 90.7, 88.6, 87.1, 85.0, 84.2, 82.1, 80.6, 79.2, 73.2,
                    72.0, 62.9, 50.4, 41.2, 33.7, 28.0, 24.1, 20.2, 17.2, 14.3, 11.4, 9.9],
            '1500': [248.6, 241.7, 204.0, 185.1, 171.4, 160.8, 154.8, 151.2, 147.6, 145.2, 141.6, 140.4, 136.8, 134.4, 132.0, 122.1,
                     120.0, 104.9, 84.0, 68.7, 56.2, 46.6, 40.1, 33.6, 28.6, 23.8, 19.0, 16.4],
            '2500': [414.3, 402.9, 340.0, 308.6, 285.7, 268.0, 258.0, 252.0, 246.0, 242.0, 236.0, 234.0, 228.0, 224.0, 220.0, 203.4,
                     200.0, 174.9, 140.0, 114.6, 93.7, 77.7, 66.9, 56.0, 47.7, 39.7, 31.7, 27.4],
        },
    },
    '2.3': {
        'temps': [38, 50, 100, 150, 200, 250, 300, 325, 350, 375, 400, 425, 454],
        'classes': {
            '150': [15.8, 15.5, 13.4, 12.1, 11.1, 10.5, 9.9, 9.3, 8.4, 7.4, 6.5, 5.5, 4.6],
            '300': [41.1, 40.5, 35.0, 31.4, 29.0, 27.4, 25.9, 25.4, 25.0, 24.7, 24.2, 23.9, 23.4],
            '400': [54.9, 53.9, 46.6, 41.9, 38.7, 36.5, 34.6, 33.9, 33.3, 33.0, 32.3, 31.8, 31.2],
            '600': [82.3, 80.9, 69.9, 62.9, 58.1, 54.7, 51.8, 50.9, 49.9, 49.4, 48.5, 47.7, 46.8],
            '900': [123.4, 121.4, 104.9, 94.3, 87.1, 82.1, 77.8, 76.3, 74.9, 74.2, 72.7, 71.6, 70.3],
            '1500': [205.7, 202.3, 174.9, 157.2, 145.2, 136.8, 129.6, 127.2, 124.8, 123.6, 121.2, 119.3, 117.1],
            '2500': [342.9, 337.1, 291.4, 262.0, 242.0, 228.0, 216.0, 212.0, 208.0, 206.0, 202.0, 198.9, 195.1],
        },
    },
}

# 材质牌号 -> ASME B16.5 材料组（群组编号固定额定曲线，不按"不锈钢"猜）
MATERIAL_PT_GROUPS = {
    'A105': '1.1',
    'A105N': '1.1',
    'LF2': '1.1',
    'A350LF2': '1.1',
    'F304': '2.1',
    'F304L': '2.3',
    'F316': '2.1',
    'F316L': '2.3',
    'F11': '1.9',
    'F22': '1.10',
    'F91': '1.15',
    'F51': None,
}

MIN_DESIGN_TEMP_C = -29  # 低于此温度需低温冲击评估
MAX_QTY_WARN = 10000
DISCOUNT_APPROVAL_PCT = Decimal('5')
DISCOUNT_MAX_PCT = Decimal('50')
CUSTOMER_DISCOUNT_MAX_PCT = Decimal('30')
VALIDITY_WARN_DAYS = 90

STATUS_PASS = 'pass'
STATUS_WARN = 'warn'
STATUS_FAIL = 'fail'
STATUS_NA = 'na'

_ALLOWED_STATUS = {STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_NA}


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _normalize_rating(rating_code) -> str:
    """把 '150' / 'Class 150' / 'cl150' 归一成 '150'。"""
    text = str(rating_code or '').strip().lower()
    for prefix in ('class', 'cl', 'class-', 'cl-'):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip().lstrip('-').strip() or text


def material_group(material_code) -> Optional[str]:
    """牌号 -> 材料组；未知返回 None（校验会显式提示"未接入"）。"""
    if material_code is None:
        return None
    code = str(material_code).strip().upper().replace(' ', '').replace('-', '')
    if code in MATERIAL_PT_GROUPS:
        return MATERIAL_PT_GROUPS[code]
    # 常见别名：ASTM A182 F316L / A105 等
    for suffix in ('ASTM',):
        code = code.replace(suffix, '')
    for prefix in ('SA', 'A'):
        if code.startswith(prefix) and code[len(prefix):] in MATERIAL_PT_GROUPS:
            return MATERIAL_PT_GROUPS[code[len(prefix):]]
    return MATERIAL_PT_GROUPS.get(code)


def pt_allowable_bar(material_group_code, rating_code, temp_c) -> dict:
    """取/插值允许工作压力，返回 {status, allowed_bar, group, rating, temp_c, method}。"""
    group = material_group_code if isinstance(material_group_code, str) and material_group_code in PT_TABLES else material_group(material_group_code)
    rating = _normalize_rating(rating_code)
    result = {
        'status': STATUS_NA,
        'allowed_bar': None,
        'material_group': group,
        'rating': rating,
        'temp_c': None if temp_c is None else float(temp_c),
        'method': None,
    }
    if group not in PT_TABLES:
        return result

    table = PT_TABLES[group]
    values = table['classes'].get(rating)
    if not values:
        return result
    if temp_c is None:
        return result

    temp = float(temp_c)
    temps = table['temps']
    if temp > temps[-1]:
        result['status'] = STATUS_FAIL
        result['method'] = 'out_of_range'
        return result

    if temp <= temps[0]:  # floor：不向下外推
        value = values[0]
        result.update(status=STATUS_PASS if value is not None else STATUS_NA,
                      allowed_bar=None if value is None else float(value), method='floor')
        return result

    index = None
    for i in range(len(temps) - 1):
        if temps[i] <= temp <= temps[i + 1]:
            index = i
            break
    if index is None:  # 不会发生（已判上界），兜底
        result['status'] = STATUS_FAIL
        result['method'] = 'out_of_range'
        return result

    low, high = values[index], values[index + 1]
    if low is None or high is None:
        result['status'] = STATUS_NA
        result['method'] = 'no_rating'
        return result

    if temp == temps[index]:
        allowed = float(low)
        method = 'exact'
    elif temp == temps[index + 1]:
        allowed = float(high)
        method = 'exact'
    else:
        span = temps[index + 1] - temps[index]
        ratio = (temp - temps[index]) / span if span else 0.0
        allowed = low + (high - low) * ratio
        method = 'interpolate'
    result.update(status=STATUS_PASS, allowed_bar=round(allowed, 3), method=method)
    return result


def _result(status, messages=None, **extra) -> dict:
    data = {'status': status, 'messages': list(messages or [])}
    data.update(extra)
    return data


def validate_pt(material_code, rating_code, temp_c, pressure_bar) -> dict:
    """压力–温度校验，返回 ``{status, allowed_bar, material_group, messages, method}``。"""
    group = material_group(material_code)
    lookup = pt_allowable_bar(group, rating_code, temp_c)
    allowed = lookup['allowed_bar']
    messages: List[str] = []

    rating = lookup['rating'] or _normalize_rating(rating_code)
    if group is None:
        messages.append(f'材质 {material_code} 未接入压力–温度表（当前仅 ASME B16.5 组 1.1/2.1/2.3）')
        return _result(STATUS_NA, messages, allowed_bar=None, material_group=None,
                       method='not_connected', rating=rating)
    if lookup['status'] == STATUS_NA and lookup['method'] == 'no_rating':
        messages.append(f'材料组 {group} Class {rating} 在 {temp_c}°C 无额定值，不得使用')
        return _result(STATUS_FAIL, messages, allowed_bar=None, material_group=group,
                       method='no_rating', rating=rating)
    if group not in PT_TABLES:
        messages.append(f'材料组 {group} 未接入压力–温度表（当前仅 1.1/2.1/2.3）')
        return _result(STATUS_NA, messages, allowed_bar=None, material_group=group,
                       method='not_connected', rating=rating)
    if lookup['status'] == STATUS_FAIL or allowed is None:
        last = PT_TABLES[group]['temps'][-1]
        messages.append(f'设计温度 {temp_c}°C 超出表内最高温度 {last}°C，该材料不可用')
        return _result(STATUS_FAIL, messages, allowed_bar=None, material_group=group,
                       method='out_of_range', rating=rating)

    allowed_dec = Decimal(str(allowed)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    pressure = None if pressure_bar is None else _dec(pressure_bar)

    if temp_c is not None and float(temp_c) < MIN_DESIGN_TEMP_C:
        messages.append(f'设计温度 {temp_c}°C 低于 {MIN_DESIGN_TEMP_C}°C，需低温冲击评估')
    if pressure is None or pressure <= 0:
        messages.append(f'{temp_c}°C 额定 {allowed_dec} bar（材料组 {group} Class {rating}）；未输入设计压力')
        return _result(STATUS_NA, messages, allowed_bar=float(allowed_dec),
                       material_group=group, method=lookup['method'], rating=rating)

    if pressure > allowed_dec:
        messages.append(
            f'不通过：{temp_c}°C 额定 {allowed_dec} bar < 设计 {pressure} bar，'
            f'建议升级压力等级或改材质（材料组 {group}）'
        )
        return _result(STATUS_FAIL, messages, allowed_bar=float(allowed_dec),
                       material_group=group, method=lookup['method'], rating=rating)

    if allowed_dec > 0 and pressure / allowed_dec > Decimal('0.9'):
        messages.append(
            f'接近额定上限：{temp_c}°C 额定 {allowed_dec} bar，设计 {pressure} bar（材料组 {group}）'
        )
        return _result(STATUS_WARN, messages, allowed_bar=float(allowed_dec),
                       material_group=group, method=lookup['method'], rating=rating)

    return _result(STATUS_PASS, messages, allowed_bar=float(allowed_dec),
                   material_group=group, method=lookup['method'], rating=rating)


# ---------------------------------------------------------------- 其它校验
def validate_combination(*, standard_id, flange_type_id, size, rating_code, row=None) -> dict:
    """标准/类型/等级/口径组合是否存在且尺寸可用。"""
    if row is None:
        label = f'{standard_id} / {flange_type_id} / {rating_code} / {size}'
        return _result(STATUS_FAIL, [f'该组合不存在：{label}'], field='size')
    messages = []
    if not getattr(row, 'od', None) or not getattr(row, 'th', None):
        messages.append('规格缺少外径/厚度，无法计算')
    return _result(STATUS_FAIL if messages else STATUS_PASS, messages, field='size')


def validate_qty(qty) -> dict:
    """数量必须是正整数；超大批量提示人工复核。"""
    try:
        value = int(qty)
    except (TypeError, ValueError):
        return _result(STATUS_FAIL, ['数量必须是整数'], field='qty')
    if value < 1:
        return _result(STATUS_FAIL, ['数量必须 ≥ 1'], field='qty')
    if value > MAX_QTY_WARN:
        return _result(STATUS_WARN, [f'数量 {value} 件超过 {MAX_QTY_WARN}，建议人工复核'], field='qty')
    return _result(STATUS_PASS, [], field='qty')


def validate_discount(discount_pct, customer_discount_pct=None) -> dict:
    """折扣 0–50%；>5% 触发审批；客户默认折扣 0–30%。"""
    try:
        value = _dec(discount_pct)
    except Exception:
        return _result(STATUS_FAIL, ['折扣格式不正确'], field='discount_pct')
    if value < 0 or value > DISCOUNT_MAX_PCT:
        return _result(STATUS_FAIL, [f'折扣必须在 0–{DISCOUNT_MAX_PCT}% 之间'], field='discount_pct')
    messages = []
    status = STATUS_PASS
    if value > DISCOUNT_APPROVAL_PCT:
        status = STATUS_WARN
        messages.append(f'折扣 {value}% 超过 {DISCOUNT_APPROVAL_PCT}%，需审批')
    if customer_discount_pct is not None:
        try:
            customer = _dec(customer_discount_pct)
        except Exception:
            customer = Decimal('0')
        if customer < 0 or customer > CUSTOMER_DISCOUNT_MAX_PCT:
            status = STATUS_FAIL
            messages.append(f'客户默认折扣必须在 0–{CUSTOMER_DISCOUNT_MAX_PCT}% 之间')
    return _result(status, messages, field='discount_pct')


def validate_validity(valid_until, on_date: Optional[date] = None) -> dict:
    """价格/报价有效期：不能早于报价日；超长有效期提示。"""
    if valid_until is None:
        return _result(STATUS_FAIL, ['缺少报价有效期'], field='valid_until')
    on_date = on_date or timezone.localdate()
    if valid_until < on_date:
        return _result(STATUS_FAIL, [f'有效期 {valid_until} 早于报价日 {on_date}'], field='valid_until')
    if valid_until - on_date > timedelta(days=VALIDITY_WARN_DAYS):
        return _result(STATUS_WARN, [f'有效期超过 {VALIDITY_WARN_DAYS} 天，建议缩短'], field='valid_until')
    return _result(STATUS_PASS, [], field='valid_until')


def combine_status(results) -> str:
    """汇总多个校验结果：fail > warn > pass；没有任何结果时为 pass。"""
    statuses = [r.get('status') for r in results if r]
    if STATUS_FAIL in statuses:
        return STATUS_FAIL
    if STATUS_WARN in statuses:
        return STATUS_WARN
    return STATUS_PASS


def validate_all(*, pt: Optional[dict] = None, checks=None) -> dict:
    """契约 §3.2 ``validation`` 结构：{status, messages, pt}。"""
    checks = list(checks or [])
    statuses = [c for c in checks if c]
    if pt is not None:
        # p/T 未接入/未输入压力属于"未完成校验"，按 warn 提示，不能静默当 pass
        pt_status = pt.get('status')
        statuses.append({'status': STATUS_WARN if pt_status == STATUS_NA else pt_status})
    status = combine_status(statuses)
    messages: List[str] = []
    for check in checks:
        if not check:
            continue
        messages.extend(check.get('messages') or [])
    if pt and pt.get('status') in (STATUS_WARN, STATUS_FAIL):
        messages.extend(pt.get('messages') or [])
    pt_output = None
    if pt is not None:
        pt_output = {
            'status': pt.get('status'),
            'allowed_bar': pt.get('allowed_bar'),
            'material_group': pt.get('material_group'),
            'method': pt.get('method'),
            'rating': pt.get('rating'),
        }
    return {'status': status, 'messages': messages, 'pt': pt_output}
