"""从参考数据里提取压力-温度（p/T）序列，并按材料组提供选择。

不同标准的表结构不一样：
- ASME：行是温度、列是 Class（c150…），每个材料组一张 SI 表；
- EN：主表是 group 3E0 的「行温度、列 PN」；PN16 另有按材料组排列的表；
- JIS：行是压力等级 + 材料组 + 分级（division），列是温度，单位 MPa。

这里只做「把表读成序列」，不做插值，也不改写标准给出的数值。
"""

import re

from .formatting import normalize_decimal, temp_label

GROUP_RE = re.compile(r'Group\s+([0-9]+(?:\.[0-9]+)?)', re.I)


def _columns(table):
    return table.get('columns') or []


def _index(columns, key):
    for i, column in enumerate(columns):
        if (column.get('key') or '') == key:
            return i
    return None


def _temp_index(columns):
    return _index(columns, 'temp')


def _temperature_rows(columns, rows, value_index, temp_key='temp'):
    temp_index = _index(columns, temp_key)
    if temp_index is None:
        return []
    output = []
    for row in rows:
        if temp_index >= len(row) or value_index >= len(row):
            continue
        raw_temp = row[temp_index]
        raw_value = row[value_index]
        if raw_value in ('', '—', '-', None):
            continue
        output.append((temp_label(raw_temp), normalize_decimal(raw_value)))
    return output


def _asme_groups(topic, rating_code):
    columns = {}
    for section in topic.get('sections', []):
        table = section.get('table')
        if not table:
            continue
        keys = [c.get('key') for c in _columns(table)]
        if {'group', 'materials', 'table'} <= set(keys):
            gi, mi = keys.index('group'), keys.index('materials')
            for row in table.get('rows') or []:
                if gi < len(row) and mi < len(row):
                    columns[row[gi].strip()] = row[mi]

    groups = []
    seen = set()
    for section in topic.get('sections', []):
        table = section.get('table')
        if not table:
            continue
        keys = [c.get('key') for c in _columns(table)]
        value_key = f'c{rating_code}'
        if 'temp' not in keys or value_key not in keys:
            continue
        caption = table.get('caption') or section.get('heading') or ''
        match = GROUP_RE.search(caption)
        if not match:
            continue
        code = match.group(1)
        if code in seen:
            continue
        series = _temperature_rows(
            _columns(table), table.get('rows') or [], keys.index(value_key)
        )
        if not series:
            continue
        seen.add(code)
        groups.append({
            'code': code,
            'label': f'材料组 {code}',
            'materials': columns.get(code, ''),
            'caption': table.get('caption', ''),
            'note': table.get('note', ''),
            'rows': series,
        })
    groups.sort(key=lambda g: tuple(int(p) for p in g['code'].split('.')))
    return groups


def _en_groups(topic, rating_code):
    groups = []
    seen = set()

    # 1) 主表：group 3E0，列是 PN
    for section in topic.get('sections', []):
        table = section.get('table')
        if not table:
            continue
        keys = [c.get('key') for c in _columns(table)]
        if 'temp' not in keys or rating_code not in keys:
            continue
        caption = table.get('caption', '')
        match = re.search(r'group\s+([0-9A-Z]+)', caption, re.I)
        if not match:
            continue
        code = match.group(1)
        series = _temperature_rows(
            _columns(table), table.get('rows') or [], keys.index(rating_code)
        )
        if series and code not in seen:
            seen.add(code)
            groups.append({
                'code': code,
                'label': f'材料组 {code}',
                'materials': '',
                'caption': caption,
                'note': table.get('note', ''),
                'rows': series,
            })

    # 2) PN16 的「按材料组」表（非奥氏体 / 奥氏体）：行是材料组、列是温度
    if rating_code == 'pn16':
        for section in topic.get('sections', []):
            table = section.get('table')
            if not table:
                continue
            keys = [c.get('key') for c in _columns(table)]
            if 'group' not in keys:
                continue
            group_index = keys.index('group')
            temp_columns = [
                (i, c) for i, c in enumerate(_columns(table))
                if (c.get('unit') or '').upper() == '°C'
            ]
            if not temp_columns:
                continue
            mat_index = keys.index('mat') if 'mat' in keys else None
            for row in table.get('rows') or []:
                if group_index >= len(row):
                    continue
                code = row[group_index].split('·')[0].strip()
                if not code or code in seen:
                    continue
                series = []
                for i, column in temp_columns:
                    if i < len(row) and row[i] not in ('', '—', '-', None):
                        series.append((temp_label(column.get('label', '')), normalize_decimal(row[i])))
                if not series:
                    continue
                seen.add(code)
                groups.append({
                    'code': code,
                    'label': f'材料组 {code}',
                    'materials': row[mat_index] if mat_index is not None and mat_index < len(row) else '',
                    'caption': table.get('caption', ''),
                    'note': table.get('note', ''),
                    'rows': series,
                })
    return groups


def _jis_groups(topic, rating_label):
    groups = []
    seen = set()
    for section in topic.get('sections', []):
        table = section.get('table')
        if not table:
            continue
        keys = [c.get('key') for c in _columns(table)]
        if 'rating' not in keys or 'reference' not in keys:
            continue
        rating_i = keys.index('rating')
        ref_i = keys.index('reference')
        spec_i = keys.index('specified') if 'specified' in keys else None
        temp_columns = [
            (i, c) for i, c in enumerate(_columns(table))
            if (c.get('unit') or '').upper() == '°C'
        ]
        wanted = rating_label.strip().upper()
        for row in table.get('rows') or []:
            if rating_i >= len(row) or str(row[rating_i]).strip().upper() != wanted:
                continue
            code = str(row[ref_i]).strip() if ref_i < len(row) else ''
            if not code or code in seen:
                continue
            series = []
            for i, column in temp_columns:
                if i < len(row) and row[i] not in ('', '—', '-', None):
                    series.append((temp_label(column.get('label', '')), normalize_decimal(row[i])))
            if not series:
                continue
            seen.add(code)
            groups.append({
                'code': code,
                'label': f'材料组 {code}',
                'materials': row[spec_i] if spec_i is not None and spec_i < len(row) else '',
                'caption': table.get('caption', ''),
                'note': table.get('note', ''),
                'rows': series,
            })
    return groups


def available_groups(doc, standard_id, rating_code, rating_label):
    if doc is None:
        return []
    topic = (doc.payload or {}).get('topics', {}).get('pressure-ratings')
    if not topic:
        return []
    try:
        if standard_id.startswith('asme'):
            return _asme_groups(topic, rating_code)
        if standard_id.startswith('en'):
            return _en_groups(topic, rating_code)
        if standard_id.startswith('jis'):
            return _jis_groups(topic, rating_label)
    except (TypeError, ValueError, IndexError):
        return []
    return []


def rating_series(doc, standard_id, rating_code, rating_label, group=None, lang='zh'):
    """返回 {'available', 'unit', 'rows', 'group', 'group_label', 'groups', ...}。"""
    groups = available_groups(doc, standard_id, rating_code, rating_label)
    if lang == 'en':
        for item in groups:
            item['label'] = item['label'].replace('材料组', 'Group')
    if not groups:
        return {'available': False, 'rows': [], 'groups': [], 'group': '', 'group_label': ''}

    default_code = '3E0' if standard_id.startswith('en') else '1.1'
    selected = next((g for g in groups if g['code'] == (group or default_code)), None)
    if selected is None:
        selected = groups[0]

    result = dict(selected)
    result.update({
        'available': True,
        'unit': 'MPa' if standard_id.startswith('jis') else 'bar',
        'group': selected['code'],
        'group_label': selected['label'],
        'groups': groups,
        'rating_label': rating_label,
    })
    first = result['rows'][0]
    result['ambient'] = f'{first[1]} {result["unit"]} @ {first[0]}'
    return result
