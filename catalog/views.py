"""页面视图：查询、标准页、规格详情、打印页、对照页、参考数据页。"""

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count
from django.db import connection
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
import logging
from django.utils import timezone

from . import search as search_mod
from . import zh_content
from .models import DimensionRow, ReferenceDoc, Standard
from .pt import rating_series

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


def drawing_svg(row, width_mm=170.0, height_mm=105.0):
    if flange_svg is None:
        return ''
    try:
        return flange_svg(row_as_dict(row), lang='zh', width_mm=width_mm, height_mm=height_mm)
    except Exception:  # pragma: no cover - 单条规格画不出来时不影响页面
        return ''


def reference_doc_for(standard):
    if not standard.reference_doc_id:
        return None
    return ReferenceDoc.objects.filter(pk=standard.reference_doc_id).first()


def topics_for(standard):
    doc = reference_doc_for(standard)
    if not doc:
        return []
    output = []
    for key, topic in doc.topics.items():
        output.append(
            {
                'key': key,
                'title': topic.get('title') or zh_content.TOPIC_LABELS.get(key, key),
                'label': zh_content.TOPIC_LABELS.get(key, key),
                'intro': (topic.get('intro') or [''])[0],
            }
        )
    return output


def home(request):
    standards = Standard.objects.annotate(row_count=Count('rows')).order_by('sort_order')

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
            quick_links.append({'label': label, 'row': row})

    context = {
        'standards': standards,
        'quick_links': quick_links,
        'search_examples': zh_content.SEARCH_EXAMPLES,
        'drawing_ready': flange_svg is not None,
    }
    return render(request, 'home.html', context)


def search(request):
    query = (request.GET.get('q') or '').strip()
    parsed = None
    results = DimensionRow.objects.none()

    if query:
        results, parsed = search_mod.search(query)
        if not parsed.chips and not parsed.keywords:
            results = DimensionRow.objects.none()
        results = results.select_related('standard', 'flange_type', 'rating').order_by(
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
        'search_examples': zh_content.SEARCH_EXAMPLES,
    }
    return render(request, 'search.html', context)


def standard_detail(request, std_id):
    standard = get_object_or_404(Standard, pk=std_id)
    base_rows = DimensionRow.objects.filter(standard=standard).select_related(
        'flange_type', 'rating'
    )

    type_stats = list(
        base_rows.values(
            'flange_type_id', 'flange_type__name_zh', 'flange_type__sort_order'
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
        'type_note': zh_content.TYPES.get(type_slug, {}).get('note_zh', ''),
        'rating_stats': rating_stats,
        'rating_code': rating_code,
        'sizes': sizes,
        'size_dn': size_dn,
        'page_obj': page,
        'total_filtered': rows.count(),
        'topics': topics_for(standard),
        'reference_doc': reference_doc_for(standard),
    }
    return render(request, 'standard_detail.html', context)


def spec_detail(request, pk):
    row = get_object_or_404(
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating'), pk=pk
    )
    standard = row.standard
    doc = reference_doc_for(standard)
    series = rating_series(doc, standard.id, row.rating.code, row.rating.label)

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
        'svg': drawing_svg(row),
        'series': series,
        'siblings': siblings,
        'previous_row': previous_row,
        'next_row': next_row,
        'same_size_types': same_size_types,
        'topics': topics_for(standard),
        'reference_doc': doc,
        'print_absolute_url': f'{settings.SITE_URL}{row.print_url()}',
    }
    return render(request, 'spec_detail.html', context)


def spec_pdf(request, pk):
    row = get_object_or_404(
        DimensionRow.objects.select_related('standard', 'flange_type', 'rating'), pk=pk
    )
    try:
        from .pdf import render_spec_pdf

        path = render_spec_pdf(row, force=request.GET.get('refresh') == '1')
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
    standard = row.standard
    doc = reference_doc_for(standard)
    series = rating_series(doc, standard.id, row.rating.code, row.rating.label)

    context = {
        'row': row,
        'standard': standard,
        'svg': drawing_svg(row, width_mm=116.0, height_mm=74.0),
        'series': series,
        'notes': zh_content.PRINT_NOTES.get(standard.id, ''),
        'disclaimer': zh_content.DISCLAIMER,
        'generated_at': timezone.localtime(),
        'print_absolute_url': f'{settings.SITE_URL}{row.print_url()}',
        'detail_absolute_url': f'{settings.SITE_URL}{row.get_absolute_url()}',
        'reference_doc': doc,
    }
    return render(request, 'spec_print.html', context)


def compare(request):
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
        'type_name': zh_content.TYPES[type_slug]['name_zh'],
        'rows': rows,
        'types': zh_content.TYPES,
        'common_dns': common_dns,
        'topics': [],
    }
    return render(request, 'compare.html', context)


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


def reference_topic(request, std_id, topic_key):
    standard = get_object_or_404(Standard, pk=std_id)
    doc = reference_doc_for(standard)
    if doc is None:
        raise Http404('该标准暂无参考数据')
    topic = doc.topics.get(topic_key)
    if not topic:
        raise Http404('参考主题不存在')

    context = {
        'standard': standard,
        'doc': doc,
        'topic': topic,
        'topic_key': topic_key,
        'topics': topics_for(standard),
    }
    return render(request, 'reference_topic.html', context)
