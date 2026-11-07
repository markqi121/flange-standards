"""批量打印：按筛选条件选中规格，合并成一个 A4 PDF。"""

import urllib.parse

from .models import DimensionRow

MAX_ROWS = 100


def parse_filters(params):
    return {
        'std': (params.get('std') or '').strip(),
        'type': (params.get('type') or '').strip(),
        'rating': (params.get('rating') or '').strip(),
        'dn_min': (params.get('dn_min') or '').strip(),
        'dn_max': (params.get('dn_max') or '').strip(),
    }


def queryset_for(filters):
    queryset = DimensionRow.objects.select_related(
        'standard', 'flange_type', 'rating'
    ).all()
    if filters['std']:
        queryset = queryset.filter(standard_id=filters['std'])
    if filters['type']:
        queryset = queryset.filter(flange_type_id=filters['type'])
    if filters['rating']:
        queryset = queryset.filter(rating__code=filters['rating'])
    try:
        if filters['dn_min']:
            queryset = queryset.filter(dn__gte=int(filters['dn_min']))
        if filters['dn_max']:
            queryset = queryset.filter(dn__lte=int(filters['dn_max']))
    except ValueError:
        pass
    return queryset.order_by(
        'standard__sort_order', 'flange_type__sort_order', 'dn', 'rating__sort_order'
    )


def query_string(filters):
    clean = {key: value for key, value in filters.items() if value}
    return urllib.parse.urlencode(clean)


def selected_rows(filters, limit=MAX_ROWS):
    return list(queryset_for(filters)[:limit])
