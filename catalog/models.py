"""法兰标准库数据模型。

设计原则：
- 一条 DimensionRow = 标准 + 法兰类型 + 公称尺寸 + 压力等级，页面和打印都按这条记录渲染；
- 参考数据（压力-温度、材料组、公差、标记、版本变更）以 JSON 原样保存在 ReferenceDoc，
  查询页按主题渲染，避免把标准表格拆碎后丢信息；
- 所有长度单位 mm，重量 kg；空值保持 NULL，不当 0 处理。
"""

from django.db import models
from django.urls import reverse


class Standard(models.Model):
    """一个标准（含系列）。ASME B16.47 的 Series A/B 是两条记录。"""

    id = models.SlugField('标准标识', primary_key=True, max_length=60)
    name = models.CharField('标准名称', max_length=80)
    name_zh = models.CharField('中文名称', max_length=140)
    short = models.CharField('简称', max_length=40)
    edition = models.CharField('标准版本', max_length=80)
    dimension_edition = models.CharField('尺寸数据版本', max_length=80, blank=True)
    region = models.CharField('适用地区', max_length=160, blank=True)
    rating_name = models.CharField('压力等级叫法', max_length=40, blank=True)
    size_syntax = models.CharField('尺寸写法', max_length=60, blank=True)
    base_material = models.CharField('基础材料', max_length=80, blank=True)
    material_options = models.JSONField('可选材料', default=list, blank=True)
    intro_zh = models.JSONField('中文简介', default=list, blank=True)
    rating_note_zh = models.TextField('压力等级说明', blank=True)
    edition_note_zh = models.TextField('版本说明', blank=True)
    reference_doc_id = models.CharField('参考数据文档', max_length=60, blank=True)
    sort_order = models.PositiveSmallIntegerField('排序', default=0)

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = '标准'
        verbose_name_plural = '标准'

    def __str__(self):
        return f'{self.name} ({self.edition})'

    @property
    def effective_edition(self):
        return self.dimension_edition or self.edition


class FlangeType(models.Model):
    """法兰类型：对焊、滑套、盲板。"""

    slug = models.SlugField('类型标识', max_length=40, primary_key=True)
    name_zh = models.CharField('中文名称', max_length=40)
    name_en = models.CharField('英文名称', max_length=60)
    note_zh = models.TextField('说明', blank=True)
    sort_order = models.PositiveSmallIntegerField('排序', default=0)

    class Meta:
        ordering = ['sort_order', 'slug']
        verbose_name = '法兰类型'
        verbose_name_plural = '法兰类型'

    def __str__(self):
        return self.name_zh


class Rating(models.Model):
    """压力等级，按标准区分（Class 150 / PN16 / 10K）。"""

    standard = models.ForeignKey(
        Standard, on_delete=models.CASCADE, related_name='ratings', verbose_name='标准'
    )
    code = models.CharField('等级代码', max_length=20)
    label = models.CharField('显示名称', max_length=40)
    sort_order = models.PositiveSmallIntegerField('排序', default=0)

    class Meta:
        ordering = ['standard', 'sort_order']
        unique_together = [('standard', 'code')]
        verbose_name = '压力等级'
        verbose_name_plural = '压力等级'

    def __str__(self):
        return f'{self.label} · {self.standard.short}'


class DimensionRow(models.Model):
    """一条规格：标准 + 类型 + 尺寸 + 压力等级。所有尺寸单位 mm，重量 kg。"""

    standard = models.ForeignKey(
        Standard, on_delete=models.CASCADE, related_name='rows', verbose_name='标准'
    )
    flange_type = models.ForeignKey(
        FlangeType, on_delete=models.PROTECT, related_name='rows', verbose_name='法兰类型'
    )
    rating = models.ForeignKey(
        Rating, on_delete=models.CASCADE, related_name='rows', verbose_name='压力等级'
    )
    size = models.CharField('尺寸键', max_length=50)
    size_label = models.CharField('尺寸显示名', max_length=50)
    dn = models.PositiveIntegerField('公称尺寸 DN (mm)')

    od = models.FloatField('外径 D')
    th = models.FloatField('厚度 C / 盲板厚度')
    rf = models.FloatField('密封面直径 D2', null=True, blank=True)
    rf_height = models.FloatField('密封面高度 f', null=True, blank=True)
    bc = models.FloatField('螺栓孔中心圆 K')
    bolts = models.PositiveSmallIntegerField('螺栓孔数 n')
    bolt_hole = models.FloatField('螺栓孔直径 d')
    hub = models.FloatField('轮毂直径 A', null=True, blank=True)
    length = models.FloatField('长度 H', null=True, blank=True)
    hub_weld = models.FloatField('焊接端直径', null=True, blank=True)
    weight = models.FloatField('近似重量 (kg)', null=True, blank=True)

    note = models.CharField('备注', max_length=200, blank=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        ordering = ['standard__sort_order', 'flange_type__sort_order', 'dn', 'rating__sort_order']
        unique_together = [('standard', 'flange_type', 'size', 'rating')]
        indexes = [
            models.Index(fields=['standard', 'flange_type', 'rating', 'dn']),
            models.Index(fields=['dn']),
            models.Index(fields=['standard', 'size']),
        ]
        verbose_name = '尺寸规格'
        verbose_name_plural = '尺寸规格'

    def __str__(self):
        return self.title_zh

    @property
    def title_zh(self):
        return f'{self.standard.short} {self.rating.label} {self.size_label} {self.flange_type.name_zh}'

    @property
    def slug_key(self):
        return f'{self.standard_id}/{self.flange_type_id}/{self.size}/{self.rating.code}'

    def get_absolute_url(self):
        return reverse('catalog:spec_detail', args=[self.pk])

    def print_url(self):
        return reverse('catalog:spec_print', args=[self.pk])


class ReferenceDoc(models.Model):
    """一个标准的参考数据文档：p/T、材料组、公差、标记、版本变更。"""

    id = models.SlugField('文档标识', primary_key=True, max_length=60)
    edition = models.CharField('版本', max_length=80)
    verified_on = models.DateField('核对日期', null=True, blank=True)
    source_document = models.TextField('来源文档', blank=True)
    payload = models.JSONField('参考数据 JSON', default=dict)

    class Meta:
        verbose_name = '参考数据文档'
        verbose_name_plural = '参考数据文档'

    def __str__(self):
        return f'{self.id} · {self.edition}'

    @property
    def topics(self):
        return (self.payload or {}).get('topics', {})


class DataImport(models.Model):
    """导入日志，用于在页面上显示数据版本与核对时间。"""

    imported_at = models.DateTimeField('导入时间', auto_now_add=True)
    source_version = models.CharField('数据包版本', max_length=40, blank=True)
    source_path = models.CharField('数据包路径', max_length=255, blank=True)
    dimension_rows = models.PositiveIntegerField('尺寸行数', default=0)
    standards_count = models.PositiveIntegerField('标准数', default=0)
    reference_docs = models.PositiveIntegerField('参考文档数', default=0)
    note = models.CharField('说明', max_length=200, blank=True)

    class Meta:
        ordering = ['-imported_at']
        verbose_name = '导入记录'
        verbose_name_plural = '导入记录'
