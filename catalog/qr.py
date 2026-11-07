"""规格卡二维码：扫码回查规格页。"""

from django.conf import settings

try:
    import segno
except Exception:  # pragma: no cover - 依赖缺失时降级
    segno = None


def spec_qr_svg(row, group=None):
    """返回内联 SVG；segno 不可用时返回空字符串。"""
    if segno is None:
        return ''
    url = settings.SITE_URL.rstrip('/') + row.get_absolute_url()
    if group:
        url += f'?group={group}'
    qr = segno.make(url, error='m', micro=False)
    svg = qr.svg_inline(scale=4, border=1, dark='#000')
    # 让二维码在卡片里按容器缩放
    svg = svg.replace('<svg ', '<svg class="qr-svg" ', 1)
    return svg
