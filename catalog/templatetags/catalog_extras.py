from django import template

from catalog import zh_content
from catalog.formatting import fmt_dim, fmt_weight

register = template.Library()


@register.filter
def dim(value):
    return fmt_dim(value)


@register.filter
def weight(value):
    return fmt_weight(value)


@register.filter
def attr(obj, name):
    return getattr(obj, name, '')


@register.filter
def topic_label(key):
    return zh_content.TOPIC_LABELS.get(key, key)


@register.filter
def get_item(mapping, key):
    if isinstance(mapping, dict):
        return mapping.get(key, '')
    return ''


@register.simple_tag
def dimension_fields(row):
    """返回规格行里所有有值的尺寸字段，供表格统一渲染。"""
    output = []
    for key, symbol, name, unit, show in zh_content.FIELDS:
        value = getattr(row, key, None)
        if value is None or value == '':
            continue
        output.append(
            {
                'key': key,
                'symbol': symbol,
                'name': name,
                'unit': unit,
                'value': fmt_weight(value) if key == 'weight' else fmt_dim(value),
            }
        )
    return output


@register.simple_tag
def rating_badge(row):
    code = row.rating.code
    if code.startswith('pn'):
        return 'PN'
    if code.endswith('k'):
        return 'K'
    return 'CL'
