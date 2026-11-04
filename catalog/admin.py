"""后台管理：尺寸规格逐条维护、参考数据只读查看。

尺寸数据是生产要用的核心数据，后台允许逐条新增/修改/筛选；
参考数据（p/T、公差、标记等）保留标准原表的 JSON 结构，后台只读，避免误改。
"""

from django.contrib import admin

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
        'od', 'th', 'bc', 'bolts', 'bolt_hole', 'weight',
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

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('standard', 'flange_type', 'rating')


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
