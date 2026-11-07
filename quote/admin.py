"""价格库 / 客户后台（Django Admin）。

覆盖契约 §2.1–2.4 的四个数据模型：Material、MaterialPrice、ProcessRate、Customer。
后台只负责价格库维护，不做成本/定价计算；标准库 ``catalog`` 保持只读。

要点：

- 列表显示关键字，支持筛选、搜索、按生效日期钻取（``date_hierarchy``）；
- 材料价与费率都带生效日期，按“报价日 ≤ 生效日期的最近一条”取值（见 prices.py）；
- 演示数据的来源统一为 ``demo``（契约 §7：禁止把演示价当真实价）；T5 已把
  ``demo`` 加入 MaterialPrice/ProcessRate 的 ``SOURCE_CHOICES``，后台可直接选择。
- 材料价/费率都带 ``source``，列表与筛选中可直接按来源区分演示价与采购/市场价。
"""

from django.contrib import admin

from .models import Customer, Material, MaterialPrice, ProcessRate


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    """材质主数据：牌号、族、密度。

    ``code`` 是 ``SlugField``（默认只接受字母/数字/下划线/短横），而 EN 牌号
    ``1.4301`` / ``1.4404`` / ``1.4571`` 含点（由 ``seed_demo`` 写入）。为让这些
    行在后台可维护，编辑已存在的含点牌号时把 ``code`` 设为只读；新增时仍用普通
    牌号（字母/数字/下划线/短横）。模型本身不改。
    """

    list_display = ('code', 'label', 'family', 'density', 'active', 'sort_order')
    list_filter = ('family', 'active')
    search_fields = ('code', 'label', 'family')
    ordering = ('sort_order', 'code')
    list_editable = ('active', 'sort_order')
    fieldsets = (
        ('牌号', {'fields': ('code', 'label', 'family')}),
        ('计价参数', {'fields': ('density',)}),
        ('展示', {'fields': ('active', 'sort_order')}),
    )

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None and '.' in (obj.code or ''):
            # EN 牌号含点，SlugField 表单校验会拒绝；只读可避免改其他字段时被拦。
            readonly.append('code')
        return readonly

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        field = getattr(form, 'base_fields', {}).get('code')
        if field is not None and obj is None:
            field.help_text = (
                '字母/数字/下划线/短横；EN 牌号如 1.4301 请用 seed_demo 写入后'
                '再回后台维护其他字段。'
            )
        return form


@admin.register(MaterialPrice)
class MaterialPriceAdmin(admin.ModelAdmin):
    """材料单价（元/kg），带生效日期与来源，按日期取值。"""

    list_display = (
        'material',
        'price_per_kg',
        'currency',
        'effective_from',
        'source',
        'note',
    )
    list_filter = ('source', 'currency', 'material__family', 'effective_from')
    search_fields = ('material__code', 'material__label', 'note')
    date_hierarchy = 'effective_from'
    ordering = ('-effective_from', 'material__sort_order', 'material__code')
    list_select_related = ('material',)
    autocomplete_fields = ('material',)
    fieldsets = (
        ('材质与价格', {'fields': ('material', 'price_per_kg', 'currency')}),
        ('生效与来源', {'fields': ('effective_from', 'source', 'note')}),
    )


@admin.register(ProcessRate)
class ProcessRateAdmin(admin.ModelAdmin):
    """工艺费率：加工/表面/检测/包装/运费/管理，含档位与生效日期。"""

    list_display = ('code', 'label', 'unit', 'rate', 'effective_from', 'source')
    list_filter = ('unit', 'source', 'effective_from')
    search_fields = ('code', 'label')
    date_hierarchy = 'effective_from'
    ordering = ('code', '-effective_from')
    fieldsets = (
        ('费率', {'fields': ('code', 'label', 'unit', 'rate')}),
        (
            '档位',
            {
                'fields': ('tiers',),
                'description': 'JSON 对象。加工费键必须是 DN 数字上限并按升序，'
                '如 {"50": 12, "100": 18, "200": 28}（services 按累计档位取用）；'
                '表面/检测/包装按选项名，如 {"wood": 35, "pmi": 25}。'
                'CSV 导入可写契约示例 "<=50" 并自动规范化为 "50"。',
            },
        ),
        ('生效与来源', {'fields': ('effective_from', 'source')}),
    )


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    """客户与默认折扣（0–30%）。"""

    list_display = (
        'code',
        'name',
        'level',
        'discount_pct',
        'currency',
        'payment_terms',
        'active',
    )
    list_filter = ('level', 'currency', 'active')
    search_fields = ('code', 'name', 'payment_terms')
    ordering = ('code',)
    fieldsets = (
        ('客户', {'fields': ('code', 'name', 'level')}),
        ('报价默认值', {'fields': ('discount_pct', 'currency', 'payment_terms')}),
        ('状态', {'fields': ('active',)}),
    )
