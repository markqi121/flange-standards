"""从参考数据里提取某个压力等级的压力-温度（p/T）序列。

不同标准的表结构不一样：
- ASME：行是温度、列是 Class（c150…），SI 表单位 bar；
- EN：行是温度（RT、100…）、列是 PN（pn6…），单位 bar；
- JIS：行是压力等级（5K…），列是温度（t120…），单位 MPa。

这里只做“把表读成序列”，不做插值，也不改写标准给出的数值。
"""

from .formatting import normalize_decimal, temp_label


def _columns(table):
    return table.get('columns') or []


def _col_index(columns, predicate):
    for index, column in enumerate(columns):
        if predicate(column):
            return index
    return None


def _temperature_rows(columns, rows, value_index):
    temp_index = _col_index(
        columns,
        lambda c: (c.get('unit') or '').upper() == '°C' or c.get('key') == 'temp',
    )
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


def _asme_series(topic, rating_code):
    for section in topic.get('sections', []):
        table = section.get('table')
        if not table:
            continue
        columns = _columns(table)
        value_index = _col_index(
            columns,
            lambda c: (c.get('key') or '') == f'c{rating_code}'
            and (c.get('unit') or '').lower() == 'bar',
        )
        if value_index is None:
            continue
        rows = _temperature_rows(columns, table.get('rows') or [], value_index)
        if rows:
            return {
                'available': True,
                'unit': 'bar',
                'rows': rows,
                'caption': table.get('caption', ''),
                'note': table.get('note', ''),
            }
    return None


def _en_series(topic, rating_code):
    for section in topic.get('sections', []):
        table = section.get('table')
        if not table:
            continue
        columns = _columns(table)
        value_index = _col_index(
            columns,
            lambda c: (c.get('key') or '') == rating_code
            and (c.get('unit') or '').lower() == 'bar',
        )
        if value_index is None:
            continue
        rows = _temperature_rows(columns, table.get('rows') or [], value_index)
        if rows:
            return {
                'available': True,
                'unit': 'bar',
                'rows': rows,
                'caption': table.get('caption', ''),
                'note': table.get('note', ''),
            }
    return None


def _jis_series(topic, rating_label):
    for section in topic.get('sections', []):
        table = section.get('table')
        if not table:
            continue
        columns = _columns(table)
        rating_index = _col_index(columns, lambda c: (c.get('key') or '') == 'rating')
        if rating_index is None:
            continue
        temp_indexes = [
            (i, c)
            for i, c in enumerate(columns)
            if (c.get('unit') or '').upper() == '°C'
        ]
        if not temp_indexes:
            continue
        wanted = rating_label.strip().upper()
        for row in table.get('rows') or []:
            if rating_index >= len(row):
                continue
            if str(row[rating_index]).strip().upper() != wanted:
                continue
            output = []
            for index, column in temp_indexes:
                if index >= len(row):
                    continue
                value = row[index]
                if value in ('', '—', '-', None):
                    continue
                output.append((temp_label(column.get('label', '')), normalize_decimal(value)))
            if output:
                return {
                    'available': True,
                    'unit': 'MPa',
                    'rows': output,
                    'caption': table.get('caption', ''),
                    'note': table.get('note', ''),
                }
    return None


def rating_series(doc, standard_id, rating_code, rating_label):
    """返回 {'available': bool, 'unit': str, 'rows': [(温度, 值)], 'caption', 'note'}。"""
    if doc is None:
        return {'available': False, 'rows': []}
    topic = (doc.payload or {}).get('topics', {}).get('pressure-ratings')
    if not topic:
        return {'available': False, 'rows': []}

    try:
        if standard_id.startswith('asme'):
            series = _asme_series(topic, rating_code)
        elif standard_id.startswith('en'):
            series = _en_series(topic, rating_code)
        elif standard_id.startswith('jis'):
            series = _jis_series(topic, rating_label)
        else:
            series = None
    except (TypeError, ValueError):
        series = None

    if not series:
        return {'available': False, 'rows': []}

    series['rating_label'] = rating_label
    first = series['rows'][0]
    series['ambient'] = f'{first[1]} {series["unit"]} @ {first[0]}'
    return series
