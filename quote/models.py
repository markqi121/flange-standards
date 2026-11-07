"""报价功能数据模型。

严格对齐 `docs/报价功能-接口契约.md` §2：

- 金额 Decimal(14,2)（材料单价 12,2、折扣 5,2、汇率 10,4），重量 Decimal(10,3)；
- 尺寸不重复存储，直接外键引用只读的 ``catalog.DimensionRow``；
- 报价单保存成本快照（CostSnapshot），历史报价不随价格库变化而改写；
- 关键业务表（报价单/明细/价格/客户）禁止物理删除（PROTECT），审计留痕在 ApprovalLog。

本文件只由 backend-core 修改；迁移文件由本任务生成。
"""

from decimal import Decimal

from django.conf import settings
from django.db import models

# 金额/重量量化常量，供其他模块复用（写在 models 里避免循环导入）
MONEY_PLACES = Decimal('0.01')
WEIGHT_PLACES = Decimal('0.001')
DEFAULT_WEIGHT_RULE = 'w0.3'


class Material(models.Model):
    """材质主数据：牌号、族、密度。"""

    code = models.SlugField('牌号代码', max_length=40, unique=True)
    label = models.CharField('展示名', max_length=100)
    family = models.CharField('材料族', max_length=40)
    density = models.DecimalField(
        '密度 g/cm³', max_digits=6, decimal_places=3, default=Decimal('7.850')
    )
    active = models.BooleanField('可选', default=True)
    sort_order = models.PositiveSmallIntegerField('排序', default=0)

    class Meta:
        ordering = ['sort_order', 'code']
        verbose_name = '材质'
        verbose_name_plural = '材质'

    def __str__(self):
        return f'{self.code} · {self.label}'


class MaterialPrice(models.Model):
    """材料单价（元/kg），按生效日期取值。"""

    SOURCE_CHOICES = [
        ('purchase', '采购'),
        ('market', '市场'),
        ('manual', '手工'),
        ('demo', '演示'),
    ]

    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name='prices', verbose_name='材质'
    )
    price_per_kg = models.DecimalField(
        '单价 元/kg', max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    currency = models.CharField('币种', max_length=3, default='CNY')
    effective_from = models.DateField('生效日期')
    source = models.CharField('来源', max_length=20, choices=SOURCE_CHOICES, default='manual')
    note = models.CharField('备注', max_length=200, blank=True)

    class Meta:
        ordering = ['material', '-effective_from']
        unique_together = [('material', 'effective_from')]
        verbose_name = '材料价格'
        verbose_name_plural = '材料价格'

    def __str__(self):
        return f'{self.material.code} {self.price_per_kg} {self.currency}/{self.effective_from}'


class ProcessRate(models.Model):
    """工艺费率：加工/表面/检测/包装/运费/管理。"""

    UNIT_CHOICES = [
        ('per_piece', '元/件'),
        ('per_kg', '元/kg'),
        ('percent', '%'),
        ('per_hour', '元/时'),
    ]
    SOURCE_CHOICES = [
        ('purchase', '采购'),
        ('market', '市场'),
        ('manual', '手工'),
        ('demo', '演示'),
    ]

    code = models.SlugField('费率代码', max_length=40, unique=True)
    label = models.CharField('展示名', max_length=60)
    unit = models.CharField('单位', max_length=20, choices=UNIT_CHOICES, default='per_piece')
    rate = models.DecimalField(
        '基础费率', max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    tiers = models.JSONField('档位', default=dict, blank=True)
    effective_from = models.DateField('生效日期')
    source = models.CharField(
        '来源', max_length=20, choices=SOURCE_CHOICES, default='manual'
    )

    class Meta:
        ordering = ['code', '-effective_from']
        verbose_name = '工艺费率'
        verbose_name_plural = '工艺费率'

    def __str__(self):
        return f'{self.code} · {self.rate} {self.get_unit_display()}'


class Customer(models.Model):
    """客户与默认折扣。"""

    LEVEL_CHOICES = [('A', 'A'), ('B', 'B'), ('C', 'C')]
    CURRENCY_CHOICES = [('CNY', 'CNY'), ('USD', 'USD')]

    code = models.SlugField('客户编码', max_length=40, unique=True)
    name = models.CharField('客户名称', max_length=120)
    level = models.CharField('等级', max_length=1, choices=LEVEL_CHOICES, default='B')
    discount_pct = models.DecimalField(
        '默认折扣 %', max_digits=5, decimal_places=2, default=Decimal('0.00')
    )
    currency = models.CharField('币种', max_length=3, choices=CURRENCY_CHOICES, default='CNY')
    payment_terms = models.CharField('账期', max_length=100, blank=True)
    active = models.BooleanField('启用', default=True)

    class Meta:
        ordering = ['code']
        verbose_name = '客户'
        verbose_name_plural = '客户'

    def __str__(self):
        return f'{self.code} · {self.name}'


class CostSnapshot(models.Model):
    """成本快照：报价时使用的全部参数与数据版本，保证可回放。"""

    params = models.JSONField('全部参数', default=dict, blank=True)
    standard_editions = models.JSONField('引用标准版本', default=dict, blank=True)
    price_versions = models.JSONField('价格生效版本', default=dict, blank=True)
    weight_rule = models.CharField('重量规则', max_length=20, default=DEFAULT_WEIGHT_RULE)
    fx_rate = models.DecimalField(
        '汇率 1USD=?CNY', max_digits=10, decimal_places=4, default=Decimal('7.1000')
    )
    tax_rate = models.DecimalField(
        '税率 %', max_digits=5, decimal_places=2, default=Decimal('13.00')
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='cost_snapshots',
        null=True,
        blank=True,
        verbose_name='创建人',
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name = '成本快照'
        verbose_name_plural = '成本快照'

    def __str__(self):
        return f'快照 #{self.pk} · {self.weight_rule} · {self.created_at:%Y-%m-%d %H:%M}'


class Quotation(models.Model):
    """报价单头。"""

    class Status(models.TextChoices):
        DRAFT = 'draft', '草稿'
        PENDING = 'pending', '待审批'
        APPROVED = 'approved', '已批准'
        SENT = 'sent', '已发送'
        ACCEPTED = 'accepted', '已接受'
        REJECTED = 'rejected', '已拒绝'
        EXPIRED = 'expired', '已过期'
        CANCELLED = 'cancelled', '已作废'
        ORDERED = 'ordered', '已转订单'

    CURRENCY_CHOICES = [('CNY', 'CNY'), ('USD', 'USD')]
    INCOTERM_CHOICES = [
        ('EXW', 'EXW'),
        ('FOB', 'FOB'),
        ('CIF', 'CIF'),
        ('CFR', 'CFR'),
        ('DDP', 'DDP'),
    ]

    number = models.CharField('报价编号', max_length=20, unique=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name='quotations',
        null=True,
        blank=True,
        verbose_name='客户',
    )
    status = models.CharField(
        '状态', max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    version = models.PositiveSmallIntegerField('版本', default=1)
    currency = models.CharField('币种', max_length=3, choices=CURRENCY_CHOICES, default='CNY')
    fx_rate = models.DecimalField(
        '汇率 1USD=?CNY', max_digits=10, decimal_places=4, default=Decimal('7.1000')
    )
    incoterm = models.CharField(
        '贸易条款', max_length=10, choices=INCOTERM_CHOICES, default='EXW'
    )
    payment_terms = models.CharField('账期', max_length=100, blank=True)
    valid_until = models.DateField('有效期至')
    subtotal = models.DecimalField(
        '不含税金额', max_digits=14, decimal_places=2, default=Decimal('0.00')
    )
    tax_amount = models.DecimalField(
        '税额', max_digits=14, decimal_places=2, default=Decimal('0.00')
    )
    total = models.DecimalField(
        '含税总额', max_digits=14, decimal_places=2, default=Decimal('0.00')
    )
    snapshot = models.ForeignKey(
        CostSnapshot,
        on_delete=models.PROTECT,
        related_name='quotations',
        null=True,
        blank=True,
        verbose_name='成本快照',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='quotations_created',
        null=True,
        blank=True,
        verbose_name='创建人',
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='quotations_approved',
        null=True,
        blank=True,
        verbose_name='审批人',
    )
    notes = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['customer', '-created_at']),
        ]
        verbose_name = '报价单'
        verbose_name_plural = '报价单'

    def __str__(self):
        return f'{self.number} V{self.version} · {self.get_status_display()}'

    @property
    def is_editable(self):
        return self.status in {self.Status.DRAFT, self.Status.PENDING}

    @property
    def is_active(self):
        return self.status in {
            self.Status.DRAFT,
            self.Status.PENDING,
            self.Status.APPROVED,
            self.Status.SENT,
            self.Status.ACCEPTED,
        }


class QuotationItem(models.Model):
    """报价单明细：一行 = 一个 catalog 规格 × 材质 × 数量。"""

    WEIGHT_SOURCE_CHOICES = [
        ('standard', '参考重量'),
        ('geometry', '几何估算'),
        ('manual', '人工输入'),
    ]
    WEIGHT_CONFIDENCE_CHOICES = [
        ('A', 'A'),
        ('B', 'B'),
        ('C', 'C'),
        ('D', 'D'),
    ]

    quotation = models.ForeignKey(
        Quotation, on_delete=models.CASCADE, related_name='items', verbose_name='报价单'
    )
    line_no = models.PositiveSmallIntegerField('行号', default=1)
    dimension_row = models.ForeignKey(
        'catalog.DimensionRow',
        on_delete=models.PROTECT,
        related_name='quotation_items',
        verbose_name='规格',
    )
    standard_edition = models.CharField('标准版本', max_length=80, blank=True)
    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name='quotation_items', verbose_name='材质'
    )
    material_price = models.DecimalField(
        '材料单价快照', max_digits=12, decimal_places=2, default=Decimal('0.00')
    )
    qty = models.PositiveIntegerField('数量', default=1)
    net_weight = models.DecimalField(
        '净重 kg', max_digits=10, decimal_places=3, default=Decimal('0.000')
    )
    weight_source = models.CharField(
        '重量来源', max_length=20, choices=WEIGHT_SOURCE_CHOICES, default='standard'
    )
    weight_confidence = models.CharField(
        '重量置信度', max_length=1, choices=WEIGHT_CONFIDENCE_CHOICES, default='C'
    )
    weight_rule = models.CharField('重量规则', max_length=20, default=DEFAULT_WEIGHT_RULE)
    cost_breakdown = models.JSONField('成本明细', default=dict, blank=True)
    unit_price = models.DecimalField(
        '含税单价', max_digits=14, decimal_places=2, default=Decimal('0.00')
    )
    amount = models.DecimalField(
        '不含税金额', max_digits=14, decimal_places=2, default=Decimal('0.00')
    )
    lead_time_days = models.PositiveSmallIntegerField('交期 天', default=0)
    process = models.JSONField('工艺/检测/包装', default=dict, blank=True)
    note = models.CharField('备注', max_length=200, blank=True)

    class Meta:
        ordering = ['quotation', 'line_no']
        unique_together = [('quotation', 'line_no')]
        verbose_name = '报价明细'
        verbose_name_plural = '报价明细'

    def __str__(self):
        return f'{self.quotation.number} #{self.line_no} {self.dimension_row_id}'


class ApprovalLog(models.Model):
    """审批/状态变更留痕。"""

    class Action(models.TextChoices):
        SUBMIT = 'submit', '提交'
        APPROVE = 'approve', '批准'
        REJECT = 'reject', '拒绝'
        SEND = 'send', '发送'
        ACCEPT = 'accept', '接受'
        ORDER = 'order', '转订单'
        EXPIRE = 'expire', '过期'
        CANCEL = 'cancel', '作废'
        REVISE = 'revise', '修订'

    quotation = models.ForeignKey(
        Quotation, on_delete=models.CASCADE, related_name='approval_logs', verbose_name='报价单'
    )
    action = models.CharField('动作', max_length=20, choices=Action.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='quote_approval_logs',
        null=True,
        blank=True,
        verbose_name='操作人',
    )
    comment = models.TextField('意见', blank=True)
    created_at = models.DateTimeField('时间', auto_now_add=True)

    class Meta:
        ordering = ['quotation', 'created_at', 'id']
        verbose_name = '审批日志'
        verbose_name_plural = '审批日志'

    def __str__(self):
        return f'{self.quotation.number} · {self.get_action_display()}'
