"""全站通用上下文：导航标准、数据版本、统计数字。"""

from django.conf import settings

from .models import DataImport, DimensionRow, ReferenceDoc, Standard


def site_context(request):
    standards = list(Standard.objects.all())
    last_import = DataImport.objects.first()
    return {
        'site_name': settings.SITE_NAME,
        'site_tagline': settings.SITE_TAGLINE,
        'site_url': settings.SITE_URL,
        'nav_standards': standards,
        'data_import': last_import,
        'total_specs': DimensionRow.objects.count(),
        'total_reference_docs': ReferenceDoc.objects.count(),
    }
