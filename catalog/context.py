"""全站通用上下文：导航标准、数据版本、统计数字。"""

from django.conf import settings
from django.utils import translation

from .models import DataImport, DimensionRow, ReferenceDoc, Standard


def _paths(request):
    path = request.path
    if path == '/en':
        zh_path = '/'
        en_path = '/en/'
    elif path.startswith('/en/'):
        zh_path = path[3:] or '/'
        en_path = path
    else:
        zh_path = path
        en_path = '/en' + path if path != '/' else '/en/'
    base = settings.SITE_URL.rstrip('/')
    return {
        'canonical_url': base + path,
        'alternate_zh': base + zh_path,
        'alternate_en': base + en_path,
        'x_default': base + en_path,
    }


def site_context(request):
    standards = list(Standard.objects.all())
    last_import = DataImport.objects.first()
    lang = 'en' if translation.get_language().startswith('en') else 'zh'
    return {
        'lang': lang,
        'is_en': lang == 'en',
        **_paths(request),
        'site_name': settings.SITE_NAME,
        'site_tagline': settings.SITE_TAGLINE,
        'site_url': settings.SITE_URL,
        'nav_standards': standards,
        'data_import': last_import,
        'total_specs': DimensionRow.objects.count(),
        'total_reference_docs': ReferenceDoc.objects.count(),
        'demo_login_hint': getattr(settings, 'DEMO_LOGIN_HINT', False),
    }
