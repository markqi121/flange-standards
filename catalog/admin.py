"""后台管理：尺寸规格逐条维护、参考数据只读查看。

尺寸数据是生产要用的核心数据，后台允许逐条新增/修改/筛选；
参考数据（p/T、公差、标记等）保留标准原表的 JSON 结构，后台只读，避免误改。
"""

import csv
import io

from django.contrib import admin, messages
from django.contrib.admin.models import LogEntry
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import path

from .models import (
    DataImport,
    DimensionRow,
    FlangeType,
    Rating,
    ReferenceDoc,
    Standard,
)

admin.site.site_header = '法兰标准库 · 后台'
admin.site.site_title = '法兰标准库'
admin.site.index_title = '数据维护'


class RatingInline(admin.TabularInline):
    model = Rating
    extra = 0


@admin.register(Standard)
class StandardAdmin(admin.ModelAdmin):
    list_display = ('name', 'name_zh', 'edition', 'base_material', 'sort_order')
    list_editable = ('sort_order',)
    search_fields = ('id', 'name', 'name_zh', 'short')
    readonly_fields = ('id',)
    inlines = [RatingInline]
    fieldsets = (
        ('基本信息', {
            'fields': ('id', 'name', 'name_zh', 'short', 'edition', 'dimension_edition', 'sort_order'),
        }),
        ('体系说明', {
            'fields': (
                'region', 'rating_name', 'size_syntax', 'base_material', 'material_options',
            ),
        }),
        ('中文展示文案', {
            'fields': ('intro_zh', 'rating_note_zh', 'edition_note_zh'),
            'description': 'JSON 数组 / 文本，用于前台标准页与打印页。',
        }),
        ('参考数据', {'fields': ('reference_doc_id',)}),
    )


@admin.register(FlangeType)
class FlangeTypeAdmin(admin.ModelAdmin):
    list_display = ('name_zh', 'slug', 'name_en', 'sort_order')
    list_editable = ('sort_order',)
    search_fields = ('slug', 'name_zh', 'name_en')


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ('standard', 'code', 'label', 'sort_order')
    list_filter = ('standard',)
    search_fields = ('code', 'label')


@admin.register(DimensionRow)
class DimensionRowAdmin(admin.ModelAdmin):
    list_display = (
        'standard', 'flange_type', 'size_label', 'dn', 'rating',
        'od', 'th', 'bc', 'bolts', 'bolt_hole', 'weight', 'view_count',
    )
    list_filter = ('standard', 'flange_type', 'rating')
    search_fields = ('size', 'size_label', 'standard__name', 'standard__short', 'rating__label')
    list_select_related = True
    ordering = ('standard', 'flange_type', 'dn', 'rating__sort_order')
    readonly_fields = ('updated_at',)
    fieldsets = (
        ('归属', {'fields': ('standard', 'flange_type', 'rating')}),
        ('公称尺寸', {'fields': ('size', 'size_label', 'dn')}),
        ('主要尺寸（mm）', {
            'fields': ('od', 'th', 'rf', 'rf_height', 'bc', 'bolts', 'bolt_hole'),
            'description': 'D 外径 / C 厚度 / D2 密封面直径 / f 密封面高度 / K 螺栓孔中心圆 / n 孔数 / d 孔径',
        }),
        ('类型相关尺寸（mm）', {
            'fields': ('hub', 'length', 'hub_weld', 'weight', 'note'),
            'description': 'A 轮毂直径 / H 轮毂长度 / B 焊接端直径 / 重量 kg。无此项留空，不要填 0。',
        }),
        ('审计', {'fields': ('updated_at',)}),
    )

    actions = ['export_csv']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('standard', 'flange_type', 'rating')

    @admin.action(description='导出所选规格为 CSV（Excel 可直接打开）')
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition'] = 'attachment; filename="flange-dimensions.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'standard', 'type', 'size', 'size_label', 'dn', 'rating',
            'od', 'th', 'rf', 'rf_height', 'bc', 'bolts', 'bolt_hole',
            'hub', 'length', 'hub_weld', 'weight', 'note',
        ])
        for row in queryset.select_related('standard', 'flange_type', 'rating').order_by('pk'):
            writer.writerow([
                row.standard_id, row.flange_type_id, row.size, row.size_label, row.dn,
                row.rating.code, row.od, row.th, row.rf, row.rf_height, row.bc,
                row.bolts, row.bolt_hole, row.hub, row.length, row.hub_weld,
                row.weight, row.note,
            ])
        return response

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('import-csv/', self.admin_site.admin_view(self.import_csv_view),
                 name='catalog_dimensionrow_import_csv'),
        ]
        return custom + urls

    def import_csv_view(self, request):
        errors = []
        imported = 0
        if request.method == 'POST':
            upload = request.FILES.get('csv_file')
            update = request.POST.get('update') == 'on'
            if not upload:
                errors.append('请选择 CSV 文件')
            elif upload.size > 5 * 1024 * 1024:
                errors.append('文件太大（上限 5 MB）')
            else:
                try:
                    text = upload.read().decode('utf-8-sig')
                    reader = csv.DictReader(io.StringIO(text))
                    required = {'standard', 'type', 'size', 'dn', 'rating', 'od', 'th', 'bc', 'bolts', 'bolt_hole'}
                    missing = required - set(reader.fieldnames or [])
                    if missing:
                        errors.append('缺少表头：' + ', '.join(sorted(missing)))
                    else:
                        rows = []
                        with transaction.atomic():
                            for index, record in enumerate(reader, start=2):
                                try:
                                    rows.append(self._import_record(record, update))
                                except Exception as exc:
                                    errors.append(f'第 {index} 行：{exc}')
                                    raise
                        imported = len(rows)
                except Exception:
                    # 具体错误已经在上面记入 errors；事务已回滚
                    pass
        context = {
            **self.admin_site.each_context(request),
            'title': '导入尺寸 CSV',
            'opts': self.model._meta,
            'errors': errors,
            'imported': imported,
        }
        return render(request, 'admin/catalog/import_csv.html', context)

    def _import_record(self, record, update):
        from .models import DimensionRow, Rating, Standard
        standard = Standard.objects.get(pk=(record.get('standard') or '').strip())
        rating = Rating.objects.get(standard=standard, code=(record.get('rating') or '').strip())
        type_slug = (record.get('type') or '').strip()
        size = (record.get('size') or '').strip()
        if not size:
            raise ValueError('size 不能为空')

        def value(name):
            raw = (record.get(name) or '').strip()
            if raw == '':
                return None
            return float(raw)

        defaults = {
            'size_label': (record.get('size_label') or size).strip(),
            'dn': int(float(record.get('dn') or 0)),
            'od': value('od'), 'th': value('th'), 'rf': value('rf'),
            'rf_height': value('rf_height'), 'bc': value('bc'),
            'bolts': int(float(record.get('bolts') or 0)), 'bolt_hole': value('bolt_hole'),
            'hub': value('hub'), 'length': value('length'), 'hub_weld': value('hub_weld'),
            'weight': value('weight'), 'note': (record.get('note') or '').strip(),
        }
        if not update:
            existing = DimensionRow.objects.filter(
                standard=standard, flange_type_id=type_slug, size=size, rating=rating
            ).first()
            if existing:
                raise ValueError('记录已存在，勾选“已存在则更新”后再导入')
        row, _ = DimensionRow.objects.update_or_create(
            standard=standard, flange_type_id=type_slug, size=size, rating=rating,
            defaults=defaults,
        )
        row.full_clean()
        row.save()
        return row


@admin.register(ReferenceDoc)
class ReferenceDocAdmin(admin.ModelAdmin):
    list_display = ('id', 'edition', 'verified_on', 'topics_count')
    search_fields = ('id', 'edition')
    readonly_fields = ('id', 'payload_preview')

    def topics_count(self, obj):
        return len(obj.topics)

    topics_count.short_description = '主题数'

    def payload_preview(self, obj):
        topics = ', '.join(obj.topics.keys())
        return topics or '—'

    payload_preview.short_description = '包含主题'

    fieldsets = (
        ('文档', {'fields': ('id', 'edition', 'verified_on', 'source_document')}),
        ('内容', {'fields': ('payload_preview',)}),
    )


@admin.register(DataImport)
class DataImportAdmin(admin.ModelAdmin):
    list_display = (
        'imported_at', 'source_version', 'dimension_rows', 'standards_count', 'reference_docs',
    )
    readonly_fields = (
        'imported_at', 'source_version', 'source_path', 'dimension_rows',
        'standards_count', 'reference_docs', 'note',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    list_display = ('action_time', 'user', 'content_type', 'object_repr', 'action_flag')
    list_filter = ('action_flag', 'content_type', 'user')
    search_fields = ('object_repr', 'change_message')
    readonly_fields = ('action_time', 'user', 'content_type', 'object_id', 'object_repr',
                       'action_flag', 'change_message')
    date_hierarchy = 'action_time'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
