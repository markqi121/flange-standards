"""页面视图：查询、标准页、规格详情、打印页、对照页、参考数据页。"""

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count
from django.db import connection
from django.db.models import Count, F
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
import logging
import os
from django.utils import timezone, translation
from xml.sax.saxutils import escape

from . import en_content, search as search_mod
from . import zh_content
from .models import DataImport, DimensionRow, ReferenceDoc, Standard
from .pt import rating_series
from .qr import spec_qr_svg
from . import batch as batch_mod
from .reference_zh import topic_zh
from .reference_zh_merge import merge_topic
from .i18n_utils import current_lang, is_en, render_page

logger = logging.getLogger(__name__)

try:  # SVG 生成器由 drawing-engineer 维护，缺失时页面降级为无图
    from .drawings import flange_svg
except Exception:  # pragma: no cover - 开发期容错
    flange_svg = None


def row_as_dict(row):
    """把 ORM 行转成绘图模块需要的普通 dict。"""
    return {
        'std': row.standard_id,
        'type': row.flange_type_id,
        'size': row.size,
        'size_label': row.size_label,
        'dn': row.dn,
        'rating': row.rating.code,
        'rating_label': row.rating.label,
        'od': row.od,
        'th': row.th,
        'rf': row.rf,
        'rf_height': row.rf_height,
        'bc': row.bc,
        'bolts': row.bolts,
        'bolt_hole': row.bolt_hole,
        'hub': row.hub,
        'length': row.length,
        'hub_weld': row.hub_weld,
        'weight': row.weight,
    }


def drawing_svg(row, width_mm=170.0, height_mm=105.0, lang='zh'):
    if flange_svg is None:
        return ''
    try:
        return flange_svg(row_as_dict(row), lang=lang, width_mm=width_mm, height_mm=height_mm)
    except Exception:  # pragma: no cover - 单条规格画不出来时不影响页面
        return ''


def reference_doc_for(standard):
    if not standard.reference_doc_id:
        return None
    return ReferenceDoc.objects.filter(pk=standard.reference_doc_id).first()


def topics_for(standard, lang='zh'):
    doc = reference_doc_for(standard)
    if not doc:
        return []
    output = []
    for key, topic in doc.topics.items():
        if lang == 'en':
            merged = merge_topic(topic, {})
            labels = en_content.TOPIC_LABELS
        else:
            merged = merge_topic(topic, topic_zh(doc.id, key))
            labels = zh_content.TOPIC_LABELS
        first_intro = merged.get('intro') or ['']
        output.append(
            {
                'key': key,
                'title': merged.get('title') or labels.get(key, key),
                'label': labels.get(key, key),
                'intro': first_intro[0] if first_intro else '',
                'has_zh': merged.get('has_zh', False),
            }
        )
    return output


def home(request):
    lang = current_lang()
    standards = Standard.objects.annotate(row_count=Count('rows')).order_by('sort_order')
    popular_specs = list(
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating')
        .filter(view_count__gt=0)
        .order_by('-view_count', 'standard__sort_order', 'dn')[:6]
    )

    quick_links = []
    for label, std_id, type_slug, size, rating_code in zh_content.QUICK_LINKS:
        row = (
            DimensionRow.objects.select_related('standard', 'flange_type', 'rating')
            .filter(
                standard_id=std_id,
                flange_type_id=type_slug,
                size=size,
                rating__code=rating_code,
            )
            .first()
        )
        if row:
            quick_links.append({'label': row.title_en if lang == 'en' else label, 'row': row})

    context = {
        'standards': standards,
        'quick_links': quick_links,
        'popular_specs': popular_specs,
        'search_examples': en_content.SEARCH_EXAMPLES if lang == 'en' else zh_content.SEARCH_EXAMPLES,
        'drawing_ready': flange_svg is not None,
    }
    return render_page(request, 'home.html', context)


def search(request):
    lang = current_lang()
    query = (request.GET.get('q') or '').strip()
    parsed = None
    results = DimensionRow.objects.none()

    std_filter = (request.GET.get('std') or '').strip()
    type_filter = (request.GET.get('type') or '').strip()
    rating_filter = (request.GET.get('rating') or '').strip()

    facet_standards = []
    facet_types = []
    facet_ratings = []

    if query:
        base_results, parsed = search_mod.search(query)
        if not parsed.chips and not parsed.keywords:
            base_results = DimensionRow.objects.none()
        base_results = base_results.select_related('standard', 'flange_type', 'rating')

        facet_standards = list(
            base_results.values('standard_id', 'standard__short')
            .annotate(count=Count('id'))
            .order_by('standard__sort_order')
        )
        facet_types = [
            {
                'code': item['flange_type_id'],
                'label': (
                    zh_content.TYPES.get(item['flange_type_id'], {}).get('name_zh', item['flange_type_id'])
                    if lang == 'zh'
                    else zh_content.TYPES.get(item['flange_type_id'], {}).get('name_en', item['flange_type_id'])
                ),
                'count': item['count'],
            }
            for item in base_results.values('flange_type_id')
            .annotate(count=Count('id'))
            .order_by('flange_type__sort_order')
        ]
        facet_ratings = list(
            base_results.values('rating__code', 'rating__label')
            .annotate(count=Count('id'))
            .order_by('rating__sort_order')
        )

        results = base_results
        if std_filter:
            results = results.filter(standard_id=std_filter)
        if type_filter:
            results = results.filter(flange_type_id=type_filter)
        if rating_filter:
            results = results.filter(rating__code=rating_filter)
        results = results.order_by(
            'standard__sort_order',
            'flange_type__sort_order',
            'dn',
            'rating__sort_order',
        )

    paginator = Paginator(results, 40)
    page = paginator.get_page(request.GET.get('page'))

    context = {
        'query': query,
        'page_obj': page,
        'total': results.count(),
        'parsed': parsed,
        'std_filter': std_filter,
        'type_filter': type_filter,
        'rating_filter': rating_filter,
        'facet_standards': facet_standards,
        'facet_types': facet_types,
        'facet_ratings': facet_ratings,
        'search_examples': en_content.SEARCH_EXAMPLES if lang == 'en' else zh_content.SEARCH_EXAMPLES,
    }
    return render_page(request, 'search.html', context)


def standard_detail(request, std_id):
    lang = current_lang()
    standard = get_object_or_404(Standard, pk=std_id)
    base_rows = DimensionRow.objects.filter(standard=standard).select_related(
        'flange_type', 'rating'
    )

    type_stats = list(
        base_rows.values(
            'flange_type_id', 'flange_type__name_zh', 'flange_type__name_en', 'flange_type__note_en', 'flange_type__sort_order'
        )
        .annotate(count=Count('id'))
        .order_by('flange_type__sort_order')
    )
    type_slug = request.GET.get('type') or (type_stats[0]['flange_type_id'] if type_stats else '')
    type_row = next((item for item in type_stats if item['flange_type_id'] == type_slug), None)

    rows = base_rows
    if type_slug:
        rows = rows.filter(flange_type_id=type_slug)

    rating_code = request.GET.get('rating') or ''
    if rating_code:
        rows = rows.filter(rating__code=rating_code)

    size_dn = request.GET.get('size') or ''
    if size_dn:
        try:
            rows = rows.filter(dn=int(size_dn))
        except ValueError:
            pass

    rating_stats = list(
        base_rows.filter(flange_type_id=type_slug)
        .values('rating__code', 'rating__label', 'rating__sort_order')
        .annotate(count=Count('id'))
        .order_by('rating__sort_order')
    )

    size_stats = list(
        base_rows.filter(flange_type_id=type_slug)
        .values('dn', 'size_label')
        .annotate(count=Count('id'))
        .order_by('dn')
    )
    # 同一 DN 可能对应多个 size_label（例如 ASME 的 NPS 写法），去重保留第一个
    seen_dn = set()
    sizes = []
    for item in size_stats:
        if item['dn'] in seen_dn:
            continue
        seen_dn.add(item['dn'])
        sizes.append(item)

    rows = rows.order_by('dn', 'rating__sort_order')
    paginator = Paginator(rows, 80)
    page = paginator.get_page(request.GET.get('page'))

    context = {
        'standard': standard,
        'type_stats': type_stats,
        'type_slug': type_slug,
        'type_row': type_row,
        'type_note': (
            zh_content.TYPES.get(type_slug, {}).get('note_zh', '')
            if lang == 'zh'
            else (type_row or {}).get('flange_type__note_en', '')
        ),
        'rating_stats': rating_stats,
        'rating_code': rating_code,
        'sizes': sizes,
        'size_dn': size_dn,
        'page_obj': page,
        'total_filtered': rows.count(),
        'topics': topics_for(standard, lang),
        'reference_doc': reference_doc_for(standard),
    }
    return render_page(request, 'standard_detail.html', context)


def spec_detail_legacy(request, pk):
    row = get_object_or_404(DimensionRow, pk=pk)
    return redirect(row.get_absolute_url(), permanent=True)


def spec_detail(request, slug):
    row = get_object_or_404(
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating'), slug=slug
    )
    lang = current_lang()
    standard = row.standard
    doc = reference_doc_for(standard)
    DimensionRow.objects.filter(pk=row.pk).update(view_count=F('view_count') + 1)
    group = request.GET.get('group') or None
    series = rating_series(doc, standard.id, row.rating.code, row.rating.label, group=group, lang=lang)
    selected_group = series.get('group') or ''
    query = f'?group={selected_group}' if selected_group else ''

    siblings = list(
        DimensionRow.objects.select_related('rating')
        .filter(
            standard=standard,
            flange_type=row.flange_type,
            size=row.size,
        )
        .exclude(pk=row.pk)
        .order_by('rating__sort_order')
    )
    previous_row = (
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating')
        .filter(
            standard=standard,
            flange_type=row.flange_type,
            rating=row.rating,
            dn__lt=row.dn,
        )
        .order_by('-dn')
        .first()
    )
    next_row = (
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating')
        .filter(
            standard=standard,
            flange_type=row.flange_type,
            rating=row.rating,
            dn__gt=row.dn,
        )
        .order_by('dn')
        .first()
    )
    same_size_types = list(
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating')
        .filter(standard=standard, size=row.size, rating=row.rating)
        .exclude(flange_type=row.flange_type)
        .order_by('flange_type__sort_order')
    )

    context = {
        'row': row,
        'standard': standard,
        'svg': drawing_svg(row, lang=lang),
        'series': series,
        'siblings': siblings,
        'previous_row': previous_row,
        'next_row': next_row,
        'same_size_types': same_size_types,
        'topics': topics_for(standard, lang),
        'reference_doc': doc,
        'page_title': row.title_en if lang == 'en' else row.title_zh,
        'print_absolute_url': f'{settings.SITE_URL}{row.print_url()}{query}',
        'print_url': f'{row.print_url()}{query}',
        'pdf_url': f'{reverse("catalog:spec_pdf", args=[row.pk])}{query}',
        'selected_group': selected_group,
    }
    return render_page(request, 'spec_detail.html', context)


def spec_pdf(request, pk):
    row = get_object_or_404(
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating'), pk=pk
    )
    group = request.GET.get('group') or None
    lang = current_lang()
    try:
        from .pdf import render_spec_pdf

        path = render_spec_pdf(row, group=group, lang=lang, force=request.GET.get('refresh') == '1')
    except Exception:
        logger.exception('PDF generation failed for row %s', pk)
        return HttpResponse(
            'PDF 生成失败，请稍后重试。桌面浏览器可以直接打印本页；手机可先刷新再试。',
            status=503,
            content_type='text/plain; charset=utf-8',
        )
    response = FileResponse(
        open(path, 'rb'),
        content_type='application/pdf',
        as_attachment=True,
        filename=f'flange-spec-{row.pk}.pdf',
    )
    response['Cache-Control'] = 'private, max-age=60'
    return response


def spec_print(request, pk):
    row = get_object_or_404(
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating'), pk=pk
    )
    lang = current_lang()
    standard = row.standard
    doc = reference_doc_for(standard)
    group = request.GET.get('group') or None
    series = rating_series(doc, standard.id, row.rating.code, row.rating.label, group=group, lang=lang)
    selected_group = series.get('group') or ''
    query = f'?group={selected_group}' if selected_group else ''

    context = {
        'row': row,
        'standard': standard,
        'svg': drawing_svg(row, width_mm=116.0, height_mm=74.0, lang=lang),
        'series': series,
        'selected_group': selected_group,
        'print_url': f'{row.print_url()}{query}',
        'pdf_url': f'{reverse("catalog:spec_pdf", args=[row.pk])}{query}',
        'qr_svg': spec_qr_svg(row, group),
        'notes': (en_content.PRINT_NOTES if lang == 'en' else zh_content.PRINT_NOTES).get(standard.id, ''),
        'disclaimer': en_content.DISCLAIMER if lang == 'en' else zh_content.DISCLAIMER,
        'generated_at': timezone.localtime(),
        'print_absolute_url': f'{settings.SITE_URL}{row.print_url()}{query}',
        'detail_absolute_url': f'{settings.SITE_URL}{row.get_absolute_url()}{query}',
        'reference_doc': doc,
        'page_title': row.title_en if lang == 'en' else row.title_zh,
    }
    return render_page(request, 'spec_print.html', context)


def compare(request):
    lang = current_lang()
    try:
        dn = int(request.GET.get('dn') or 100)
    except ValueError:
        dn = 100
    type_slug = request.GET.get('type') or 'weld-neck'
    if type_slug not in zh_content.TYPES:
        type_slug = 'weld-neck'

    rows = (
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating')
        .filter(dn=dn, flange_type_id=type_slug)
        .order_by('standard__sort_order', 'rating__sort_order')
    )

    dns = list(DimensionRow.objects.values_list('dn', flat=True).distinct().order_by('dn'))
    common_dns = [
        value for value in dns
        if value in {15, 20, 25, 32, 40, 50, 65, 80, 100, 150, 200, 250, 300, 400,
                     500, 600, 650, 800, 1000, 1200}
    ]

    context = {
        'dn': dn,
        'type_slug': type_slug,
        'type_name': (
            zh_content.TYPES[type_slug]['name_zh']
            if current_lang() == 'zh'
            else zh_content.TYPES[type_slug]['name_en']
        ),
        'rows': rows,
        'types': zh_content.TYPES,
        'common_dns': common_dns,
        'topics': [],
    }
    return render_page(request, 'compare.html', context)


def sitemap_xml(request):
    base = settings.SITE_URL.rstrip('/')
    last_import = DataImport.objects.first()
    lastmod = last_import.imported_at.date().isoformat() if last_import else timezone.localdate().isoformat()
    entries = []
    def add(zh_path, en_path):
        entries.append((zh_path, en_path))
    add('/', '/en/')
    add('/compare/', '/en/compare/')
    add('/batch/', '/en/batch/')
    for standard in Standard.objects.all():
        add(f'/{standard.id}/', f'/en/{standard.id}/')
        if standard.reference_doc_id:
            doc = ReferenceDoc.objects.filter(pk=standard.reference_doc_id).first()
            if doc:
                for topic in doc.topics.keys():
                    add(
                        f'/{standard.id}/ref/{topic}/',
                        f'/en/{standard.id}/ref/{topic}/',
                    )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ]
    for zh_path, en_path in entries:
        zh_url = base + zh_path
        en_url = base + en_path
        lines.append('<url>')
        lines.append(f'<loc>{escape(zh_url)}</loc>')
        lines.append(f'<xhtml:link rel="alternate" hreflang="zh-Hans" href="{escape(zh_url)}"/>')
        lines.append(f'<xhtml:link rel="alternate" hreflang="en" href="{escape(en_url)}"/>')
        lines.append(f'<xhtml:link rel="alternate" hreflang="x-default" href="{escape(en_url)}"/>')
        lines.append(f'<lastmod>{lastmod}</lastmod>')
        lines.append('</url>')
        lines.append('<url>')
        lines.append(f'<loc>{escape(en_url)}</loc>')
        lines.append(f'<xhtml:link rel="alternate" hreflang="zh-Hans" href="{escape(zh_url)}"/>')
        lines.append(f'<xhtml:link rel="alternate" hreflang="en" href="{escape(en_url)}"/>')
        lines.append(f'<xhtml:link rel="alternate" hreflang="x-default" href="{escape(en_url)}"/>')
        lines.append(f'<lastmod>{lastmod}</lastmod>')
        lines.append('</url>')
    lines.append('</urlset>')
    return HttpResponse('\n'.join(lines), content_type='application/xml; charset=utf-8')


def robots_txt(request):
    base = settings.SITE_URL.rstrip('/')
    lines = [
        'User-agent: *',
        'Allow: /',
        'Disallow: /admin/',
        'Disallow: /spec/*/print/',
        'Disallow: /spec/*/pdf/',
        'Disallow: /batch/print/',
        'Disallow: /healthz',
        '',
        f'Sitemap: {base}/sitemap.xml',
    ]
    return HttpResponse('\n'.join(lines), content_type='text/plain; charset=utf-8')


def healthz(request):
    """给 Nginx / 监控用的健康检查，不暴露任何业务数据。"""
    try:
        connection.ensure_connection()
        database = 'ok'
    except Exception:
        database = 'error'
    status = 200 if database == 'ok' else 503
    return JsonResponse(
        {'status': 'ok' if status == 200 else 'degraded', 'database': database},
        status=status,
    )


def batch(request):
    from .models import Rating
    lang = current_lang()
    filters = batch_mod.parse_filters(request.GET)
    queryset = batch_mod.queryset_for(filters)
    total = queryset.count()
    rows = list(queryset[:10])
    context = {
        'filters': filters,
        'query_string': batch_mod.query_string(filters),
        'total': total,
        'max_rows': batch_mod.MAX_ROWS,
        'rows': rows,
        'standards': Standard.objects.order_by('sort_order'),
        'types': zh_content.TYPES,
        'ratings': Rating.objects.select_related('standard').order_by(
            'standard__sort_order', 'sort_order'
        ),
        'lang': lang,
    }
    return render_page(request, 'batch.html', context)


def batch_print(request):
    lang = current_lang()
    filters = batch_mod.parse_filters(request.GET)
    rows = batch_mod.selected_rows(filters)
    if not rows:
        return HttpResponse('没有匹配的规格，请调整筛选条件。', status=404,
                            content_type='text/plain; charset=utf-8')
    items = []
    for row in rows:
        doc = reference_doc_for(row.standard)
        series = rating_series(doc, row.standard.id, row.rating.code, row.rating.label, lang=lang)
        group = series.get('group') or ''
        items.append({
            'row': row,
            'standard': row.standard,
            'series': series,
            'svg': drawing_svg(row, width_mm=116.0, height_mm=74.0, lang=lang),
            'qr_svg': spec_qr_svg(row, group),
            'notes': (en_content.PRINT_NOTES if lang == 'en' else zh_content.PRINT_NOTES).get(row.standard_id, ''),
            'disclaimer': en_content.DISCLAIMER if lang == 'en' else zh_content.DISCLAIMER,
            'reference_doc': doc,
            'print_absolute_url': f'{settings.SITE_URL}{row.print_url()}?group={group}' if group else f'{settings.SITE_URL}{row.print_url()}',
            'detail_absolute_url': f'{settings.SITE_URL}{row.get_absolute_url()}',
        })
    context = {
        'items': items,
        'generated_at': timezone.localtime(),
        'disclaimer': en_content.DISCLAIMER if lang == 'en' else zh_content.DISCLAIMER,
        'count': len(items),
        'lang': lang,
    }
    return render_page(request, 'batch_print.html', context)


def batch_pdf(request):
    lang = current_lang()
    filters = batch_mod.parse_filters(request.GET)
    query = batch_mod.query_string(filters)
    if batch_mod.queryset_for(filters).count() == 0:
        return HttpResponse('没有匹配的规格。', status=404, content_type='text/plain; charset=utf-8')
    try:
        from .pdf import render_batch_pdf

        path = render_batch_pdf(query, lang=lang)
    except Exception:
        logger.exception('Batch PDF failed')
        return HttpResponse('批量 PDF 生成失败，请减少规格数量后重试。', status=503,
                            content_type='text/plain; charset=utf-8')
    handle = open(path, 'rb')
    try:
        os.unlink(path)  # 批量 PDF 不缓存；打开后删除，避免临时文件堆积（Linux/macOS）
    except OSError:
        pass
    response = FileResponse(
        handle, content_type='application/pdf', as_attachment=True,
        filename=f'flange-batch-{timezone.localdate():%Y%m%d}.pdf',
    )
    response['Cache-Control'] = 'private, max-age=60'
    return response


def reference_topic(request, std_id, topic_key):
    lang = current_lang()
    standard = get_object_or_404(Standard, pk=std_id)
    doc = reference_doc_for(standard)
    if doc is None:
        raise Http404('该标准暂无参考数据')
    raw_topic = doc.topics.get(topic_key)
    if not raw_topic:
        raise Http404('参考主题不存在')
    topic = merge_topic(raw_topic, {}) if lang == 'en' else merge_topic(raw_topic, topic_zh(doc.id, topic_key))

    context = {
        'standard': standard,
        'doc': doc,
        'topic': topic,
        'topic_key': topic_key,
        'topics': topics_for(standard, lang),
    }
    return render_page(request, 'reference_topic.html', context)
