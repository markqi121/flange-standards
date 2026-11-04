"""法兰标准库 · 尺寸标注 SVG 生成器
====================================

根据一行法兰尺寸数据生成一张**黑白线稿、可直接内嵌 HTML** 的尺寸示意图。
整张图由左右两个视图组成，两视图共用同一比例尺：

* 左：端面视图（正视）——外圆 D、螺栓孔中心圆 K（点划线）、均布螺栓孔、
  密封面圆 RF、内孔、中心线；
* 右：全剖侧视图——法兰厚度 C、密封面凸台、轮毂直径 A、长度 H、焊接端直径；
  实体区域画 45° 剖面线，螺栓孔用虚线示意，中心线只画在轴线处。

版面（默认 170×105 mm 设计画布，可传任意 ``width_mm``/``height_mm``）

* 端面视图：D 放上方（尺寸线 + 尺寸界线），K 放右侧（两行引线文字），
  螺栓孔 ``n×Ød`` 放下方（引线指向一个孔），密封面 RF 放左下；
  所有文字都在零件轮廓之外，引线自动避让螺栓孔、中心线、尺寸线；
* 剖视图：C 尺寸线在上方、H 尺寸线在下方、f 用左上引线、A / 焊接端用右侧
  引线，四个标注分布在不同方向；
* 大图小图同一套相对布局；画布变小时自动补偿字号，保证打印后字号仍接近
  3 mm。

用法::

    from catalog.drawings import flange_svg

    row = {...}                       # 一行尺寸数据（dict）
    svg = flange_svg(row)             # 中文标注
    svg_en = flange_svg(row, lang="en")
    svg_print = flange_svg(row, width_mm=116, height_mm=74)   # 打印侧栏

    # 直接内嵌：{{ svg|safe }} / f"<div>{svg}</div>"
    # 独立文件：open("a.svg", "w").write(svg)

约束
----
* 纯标准库；不读文件、不联网、不依赖 Django；不修改传入的 row；
* 返回完整、独立、可被 ``xml.etree.ElementTree`` 解析的 ``<svg>`` 字符串
  （无 XML 声明，方便内嵌；根元素带 ``xmlns``，也可直接存成 .svg）；
* 长度单位 mm，viewBox 用 mm；白底黑线、无渐变 / 外链 / 字体文件 / script；
* 缺失字段（None 或键不存在）自动省略对应图形和标注，不抛异常；
  极扁 / 极窄 / 超大规格也能排版，数值不会出现 NaN / Infinity；
* od 缺失时返回一张带提示文字的合法空图。

字段约定（见 flange-standards-data/docs/fields.md）
---------------------------------------------------
必填：``od``（外径 D）、``th``（厚度）、``bc``（螺栓孔中心圆 K）。
可选：``rf`` / ``rf_height`` / ``bolts`` / ``bolt_hole`` / ``hub`` /
``length`` / ``hub_weld`` / ``weight`` 等；``weight`` 不参与绘图。

近似说明
--------
* 侧视图长度按标准表 ``length``（法兰面到轮毂端的总长）绘制；
* 内孔直径无法从数据直接得到：对焊端按 ``0.90×hub_weld`` 近似，
  滑套 / 无焊接端按公称直径近似，仅用于示意，不标注；
* 盲板无轮毂、无内孔，侧视图只画圆盘 + 密封面凸台 + 厚度。
"""

from __future__ import annotations

import math
import unicodedata
from xml.sax.saxutils import escape as _xml_escape

__all__ = ["flange_svg"]

# ============================================================
# 1. 常量（设计画布 170 mm × 105 mm，单位 mm）
# ============================================================

DESIGN_W = 170.0
DESIGN_H = 105.0

#: 字体族：只用系统字体名，不引用字体文件
FONT_FAMILY = (
    "'PingFang SC','Hiragino Sans GB','Microsoft YaHei',"
    "'Noto Sans CJK SC','Source Han Sans SC',sans-serif"
)

# 基础线宽（会随画布尺寸做小幅补偿）
LW_OUTLINE = 0.62   # 零件轮廓
LW_INNER = 0.58     # 可见内轮廓（RF 圆、内孔、螺栓孔）
LW_THIN = 0.30      # 尺寸线 / 尺寸界线 / 中心线
LW_HIDDEN = 0.42    # 不可见轮廓（螺栓孔虚线）
LW_HATCH = 0.24     # 剖面线

# 基础字号（会按画布尺寸补偿，保证 116×74 打印时仍约 3 mm）
FS_TITLE = 3.60
FS_VIEW = 3.35
FS_DIM = 3.40
FS_NOTE = 3.30

# 线型
DASH_CENTER = "4.0 1.2 0.9 1.2"
DASH_HIDDEN = "2.4 1.3"

# 箭头（随字号缩放）
ARROW_LEN = 2.15
ARROW_HALF = 0.55

# 版面：左右两个视图区域
CANVAS_L = 3.0
CANVAS_R = 167.0
FRONT_L = 3.0
FRONT_R = 96.0
SIDE_L = 100.0
SIDE_R = 167.0
SIDE_W = SIDE_R - SIDE_L

CY = 52.0            # 两视图共同的圆心 / 轴线 y（垂直居中、用满画布）
R_OD_MAX = 40.0      # 端面视图最大绘制半径（实际由上下标注带决定）
R_OD_MIN = 12.0      # 最小绘制半径（极端规格兜底）
TITLE_Y = 5.5        # 标题基线
CAPTION_Y = 95.0     # 视图名称基线（放在图下方，给图形让出画布高度）
NOTE_Y = 102.0       # 底部比例 / 单位注释

# 端面视图内边距（画布左边界到外圆的最大/最小留白）
FRONT_PAD_L = 4.0
FRONT_PAD_R = 2.0

# 剖视图下方 H 尺寸线相对外圆底部的距离
H_GAP = 6.0
# 剖视图上方 C 尺寸线相对外圆顶部的距离
C_GAP = 2.8
# 端面视图下方引线文字相对外圆底部的距离
BOTTOM_TEXT_GAP = 4.0
# 图形自身到标题 / 注释之间必须保留的空隙（mm）
TOP_CLEARANCE = 1.5
BOTTOM_CLEARANCE = 0.8

STD_NAMES = {
    "asme-b16-5": "ASME B16.5",
    "asme-b16-47-a": "ASME B16.47 Series A",
    "asme-b16-47-b": "ASME B16.47 Series B",
    "jis-b2220": "JIS B2220",
    "en-1092-1": "EN 1092-1",
}

TYPE_NAMES = {
    "zh": {
        "weld-neck": "对焊法兰",
        "slip-on": "滑套法兰",
        "blind": "盲板法兰",
    },
    "en": {
        "weld-neck": "Weld Neck Flange",
        "slip-on": "Slip-On Flange",
        "blind": "Blind Flange",
    },
}

# ============================================================
# 2. 多语言文案
# ============================================================


def _labels(lang: str) -> dict:
    """返回指定语言的文案表（不认识的 lang 回落到中文）。"""
    if str(lang or "").strip().lower().startswith("en"):
        key = "en"
    else:
        key = "zh"
    base = {
        "zh": {
            "od": "外径 D {}",
            "bc_name": "螺栓孔中心圆",
            "bc_value": "K {}",
            "hole": "{}×Ø{}",
            "rf": "密封面 Ø {}",
            "th": "厚度 C {}",
            "f": "密封面高度 f {}",
            "hub": "轮毂 Ø A {}",
            "length": "长度 H {}",
            "weld": "焊接端 Ø {}",
            "view_front": "a) 端面视图",
            "view_section": "b) 剖视图（半剖）",
            "view_section_full": "b) 剖视图（全剖）",
            "scale_note": "两视图同一比例 {}；示意图，尺寸以标准表为准",
            "unit": "单位：mm",
            "empty": "尺寸数据不完整，无法生成图形",
            "note_plate": "板式结构（无轮毂）",
            "note_nohub": "数据未提供轮毂尺寸",
            "note_nolen": "数据未提供轮毂长度",
        },
        "en": {
            "od": "O.D. D {}",
            "bc_name": "Bolt circle",
            "bc_value": "K {}",
            "hole": "{}×Ø{}",
            "rf": "Raised face Ø {}",
            "th": "Thickness C {}",
            "f": "RF height f {}",
            "hub": "Hub Ø A {}",
            "length": "Length H {}",
            "weld": "Weld end Ø {}",
            "view_front": "a) Front view",
            "view_section": "b) Section view (half)",
            "view_section_full": "b) Section view (full)",
            "scale_note": "Both views share one scale ({}); schematic only",
            "unit": "Unit: mm",
            "empty": "Incomplete dimension data; drawing unavailable",
            "note_plate": "Plate type (no hub)",
            "note_nohub": "No hub data",
            "note_nolen": "Hub length not provided",
        },
    }[key]
    base["type_names"] = TYPE_NAMES[key]
    base["lang_key"] = key
    return base


# ============================================================
# 3. 主题（字号 / 线宽随画布尺寸补偿）
# ============================================================


def _theme(width_mm: float, height_mm: float) -> dict:
    """按画布尺寸生成绘图主题。

    设计稿是 170×105 mm。画布变小时整体会等比缩小，字号会跟着变小；
    这里对字号/线宽做一次反向补偿，让 116×74 打印侧栏中的字仍在 3 mm 左右。
    """
    paper = min(width_mm / DESIGN_W, height_mm / DESIGN_H)
    paper = max(0.35, min(2.5, paper))
    comp = 3.2 / (FS_DIM * paper)
    comp = max(0.90, min(1.30, comp))
    return {
        "comp": comp,
        "paper": paper,
        "fs_title": FS_TITLE * comp,
        "fs_view": FS_VIEW * comp,
        "fs_dim": FS_DIM * comp,
        "fs_note": FS_NOTE * comp,
        "lw_outline": LW_OUTLINE * comp,
        "lw_inner": LW_INNER * comp,
        "lw_thin": LW_THIN * comp,
        "lw_hidden": LW_HIDDEN * comp,
        "lw_hatch": LW_HATCH * comp,
        "arrow_len": ARROW_LEN * comp,
        "arrow_half": ARROW_HALF * comp,
        "hatch_step": 1.7 * comp,
        "exit_gap": 0.6 * comp,
    }


# ============================================================
# 4. 数值 / 文本工具
# ============================================================


def _finite(value):
    """转成有限浮点数；非法值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _positive(value):
    """正数有限值，否则 None。"""
    num = _finite(value)
    if num is None or num <= 0:
        return None
    return num


def _display(value):
    """把标准表里的数值原样转成文字（不额外四舍五入）。

    int 原样；float 保留原精度（``2.0`` → ``"2.0"``，``279.4`` → ``"279.4"``）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    num = _finite(value)
    if num is None or num <= 0:
        return None
    if isinstance(value, float) and num.is_integer():
        return f"{num:.1f}"
    if num.is_integer():
        return str(int(num))
    return f"{num:.6f}".rstrip("0").rstrip(".") or "0"


def _num(value) -> str:
    """SVG 坐标格式化：固定两位小数，杜绝 nan/inf。"""
    val = _finite(value)
    if val is None:
        val = 0.0
    return f"{val:.2f}"


def _canvas(value, default: float) -> float:
    """画布尺寸清洗。"""
    num = _finite(value)
    if num is None or num < 30.0:
        return default
    return num


def _esc(text) -> str:
    return _xml_escape(str(text))


def _text_width(text, size: float) -> float:
    """估算文字宽度（mm），宁可估宽，避免标注重叠。"""
    width = 0.0
    for ch in str(text):
        if ch.isspace():
            width += 0.33
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 1.0
        elif ch in "×Ø":
            width += 0.78
        elif ch.isdigit():
            width += 0.56
        elif ch.isupper():
            width += 0.68
        elif ch.islower():
            width += 0.56
        elif ch in ".,:;":
            width += 0.30
        elif ch in "-+":
            width += 0.40
        elif ch == "″":
            width += 0.35
        else:
            width += 0.62
    return width * size


# ============================================================
# 5. SVG 图元
# ============================================================


def _line(x1, y1, x2, y2, *, w, dash=None, cap="butt") -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{_num(x1)}" y1="{_num(y1)}" x2="{_num(x2)}" y2="{_num(y2)}" '
        f'stroke-width="{w:.2f}" stroke-linecap="{cap}"{dash_attr}/>'
    )


def _circle(cx, cy, r, *, w, dash=None, fill="none") -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<circle cx="{_num(cx)}" cy="{_num(cy)}" r="{_num(r)}" '
        f'fill="{fill}" stroke-width="{w:.2f}"{dash_attr}/>'
    )


def _poly_path(points, *, close=False, fill="none", w) -> str:
    if not points:
        return ""
    d = "M" + " L".join(f"{_num(x)},{_num(y)}" for x, y in points)
    if close:
        d += " Z"
    return f'<path d="{d}" fill="{fill}" stroke-width="{w:.2f}"/>'


def _text(x, y, content, *, size, anchor="middle", weight=None) -> str:
    weight_attr = f' font-weight="{weight}"' if weight else ""
    return (
        f'<text x="{_num(x)}" y="{_num(y)}" font-size="{size:.2f}" '
        f'text-anchor="{anchor}" fill="#000" stroke="none"{weight_attr}>'
        f"{_esc(content)}</text>"
    )


def _arrow(tip, back, th) -> str:
    """实心三角箭头：tip 尖端，back 底边中点。"""
    tx, ty = tip
    bx, by = back
    dx, dy = tx - bx, ty - by
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return ""
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    half = th["arrow_half"]
    p1 = (bx + px * half, by + py * half)
    p2 = (bx - px * half, by - py * half)
    return (
        f'<path d="M{_num(tx)},{_num(ty)} '
        f'L{_num(p1[0])},{_num(p1[1])} L{_num(p2[0])},{_num(p2[1])} Z" '
        f'fill="#000" stroke="none"/>'
    )


# ============================================================
# 6. 几何判定（引线避让用）
# ============================================================


def _seg_point_dist(a, b, p) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _seg_cross(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1 = orient(a, b, c)
    o2 = orient(a, b, d)
    o3 = orient(c, d, a)
    o4 = orient(c, d, b)
    return (o1 * o2 < 0.0) and (o3 * o4 < 0.0)


def _seg_seg_dist(a, b, c, d) -> float:
    if _seg_cross(a, b, c, d):
        return 0.0
    return min(
        _seg_point_dist(a, b, c),
        _seg_point_dist(a, b, d),
        _seg_point_dist(c, d, a),
        _seg_point_dist(c, d, b),
    )


def _seg_hits_rect(a, b, rect, margin=0.0) -> bool:
    x1, y1, x2, y2 = rect
    x1 -= margin
    y1 -= margin
    x2 += margin
    y2 += margin
    if x2 <= x1 or y2 <= y1:
        return False

    def inside(p):
        return x1 <= p[0] <= x2 and y1 <= p[1] <= y2

    if inside(a) or inside(b):
        return True
    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    for i in range(4):
        if _seg_cross(a, b, corners[i], corners[(i + 1) % 4]):
            return True
    return False


def _rect(x, y, w, anchor, fs):
    """由文字锚点生成包围盒 (x1, y1, x2, y2)。"""
    if anchor == "middle":
        left = x - w / 2.0
    elif anchor == "end":
        left = x - w
    else:
        left = x
    return (left, y - 0.95 * fs, left + w, y + 0.30 * fs)


def _ang_dist(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


# ============================================================
# 7. 尺寸线 / 引线
# ============================================================


def _dim_h(parts, th, x1, x2, y, label, *, ext_ys=None, text_side="above",
           text_x=None):
    """水平线性尺寸标注。

    x1/x2 为尺寸线两端，y 为尺寸线高度，ext_ys 为尺寸界线起点（特征点）的
    y（标量或 (左, 右)），text_side 取 "above" / "below" / "right"。
    """
    fs = th["fs_dim"]
    if x2 < x1:
        x1, x2 = x2, x1
        if isinstance(ext_ys, (tuple, list)):
            ext_ys = (ext_ys[1], ext_ys[0])
    span = x2 - x1
    gap = th["exit_gap"]
    over = 0.45 * fs

    # 尺寸界线
    if ext_ys is not None:
        if isinstance(ext_ys, (tuple, list)):
            ey1, ey2 = ext_ys[0], ext_ys[1]
        else:
            ey1 = ey2 = ext_ys
        for x, ey in ((x1, ey1), (x2, ey2)):
            eyv = _finite(ey)
            if eyv is None:
                continue
            if eyv > y:      # 特征在尺寸线下方 → 界线向上
                parts.append(_line(x, eyv - gap, x, y - over, w=th["lw_thin"]))
            elif eyv < y:    # 特征在尺寸线上方 → 界线向下
                parts.append(_line(x, eyv + gap, x, y + over, w=th["lw_thin"]))

    # 尺寸线 + 箭头（短尺寸箭头外置）
    alen = th["arrow_len"]
    if span < 4.0 * fs:
        parts.append(_line(x1 - alen - 1.0, y, x2 + alen + 1.0, y, w=th["lw_thin"]))
        parts.append(_arrow((x1, y), (x1 - alen, y), th))
        parts.append(_arrow((x2, y), (x2 + alen, y), th))
    else:
        parts.append(_line(x1, y, x2, y, w=th["lw_thin"]))
        parts.append(_arrow((x1, y), (x1 + alen, y), th))
        parts.append(_arrow((x2, y), (x2 - alen, y), th))

    if text_x is None:
        text_x = (x1 + x2) / 2.0
    if text_side == "above":
        parts.append(_text(text_x, y - 0.75 * fs, label, size=fs))
    elif text_side == "below":
        parts.append(_text(text_x, y + 1.45 * fs, label, size=fs))
    elif text_side == "right":
        parts.append(_text(max(x1, x2) + 1.1 * fs, y + 0.35 * fs, label,
                           size=fs, anchor="start"))


def _leader_target(text_x, text_y, width, anchor, fs):
    """引线末端（靠近文字的一侧）坐标。"""
    if anchor == "end":
        return (text_x - width - 1.2, text_y - 0.35 * fs)
    return (text_x - 1.2, text_y - 0.35 * fs)


def _check_leader(target, feature, avoid, th):
    """评引线本身的冲突数：螺栓孔、中心线、尺寸线、其他文字框。"""
    seg = (target, feature)
    bad = 0
    for center, radius in avoid["circles"]:
        if _seg_point_dist(target, feature, center) < radius:
            bad += 1
    for a, b, margin in avoid["segments"]:
        if _seg_seg_dist(target, feature, a, b) < margin:
            bad += 1
    for rect in avoid["rects"]:
        if _seg_hits_rect(target, feature, rect, 0.4):
            bad += 1
    return bad


def _draw_leader(parts, th, feature, text_x, text_y, label, anchor, avoid,
                 prefer_deg, radius_center=None):
    """画引线 + 箭头 + 文字。

    若给出 ``radius_center``（端面视图圆心），则在圆上自动挑选一个
    “最安全”的角度作为箭头落点：不压螺栓孔、不穿中心线 / 尺寸线 /
    其他文字。否则直接连到给定 feature。
    """
    fs = th["fs_dim"]
    width = _text_width(label, fs)
    target = _leader_target(text_x, text_y, width, anchor, fs)
    if radius_center is not None:
        cx, cy, radius = radius_center
        best = None
        for deg in range(0, 360, 5):
            rad = math.radians(deg)
            feat = (cx + radius * math.cos(rad), cy - radius * math.sin(rad))
            bad = _check_leader(target, feat, avoid, th)
            score = bad * 1000.0 + _ang_dist(deg, prefer_deg) * 0.6
            score += math.hypot(feat[0] - target[0], feat[1] - target[1]) * 0.01
            if best is None or score < best[0]:
                best = (score, feat)
        feature = best[1]

    parts.append(_line(target[0], target[1], feature[0], feature[1],
                       w=th["lw_thin"]))
    dx, dy = feature[0] - target[0], feature[1] - target[1]
    length = math.hypot(dx, dy)
    if length > 1e-6:
        ux, uy = dx / length, dy / length
        alen = th["arrow_len"]
        parts.append(_arrow(feature, (feature[0] - ux * alen,
                                      feature[1] - uy * alen), th))
    parts.append(_text(text_x, text_y, label, size=fs, anchor=anchor))
    return (target, feature)


# ============================================================
# 8. 读入一行尺寸数据
# ============================================================


def _read_spec(row) -> "dict | None":
    """把原始行整理成绘图用的 spec；``od`` 缺失时返回 None。"""
    if not isinstance(row, dict):
        return None
    od = _positive(row.get("od"))
    if od is None:
        return None

    spec = {
        "od": od,
        "od_s": _display(row.get("od")),
        "th": _positive(row.get("th")),
        "th_s": _display(row.get("th")),
        "bc": _positive(row.get("bc")),
        "bc_s": _display(row.get("bc")),
        "rf": _positive(row.get("rf")),
        "rf_s": _display(row.get("rf")),
        "rf_height": _positive(row.get("rf_height")),
        "rf_height_s": _display(row.get("rf_height")),
        "hub": _positive(row.get("hub")),
        "hub_s": _display(row.get("hub")),
        "length": _positive(row.get("length")),
        "length_s": _display(row.get("length")),
        "hub_weld": _positive(row.get("hub_weld")),
        "hub_weld_s": _display(row.get("hub_weld")),
        "bolt_hole": _positive(row.get("bolt_hole")),
        "bolt_hole_s": _display(row.get("bolt_hole")),
        "bolts": None,
        "dn": None,
        "std": str(row.get("std") or "").strip(),
        "size": str(row.get("size") or "").strip(),
        "size_label": str(row.get("size_label") or "").strip(),
        "rating_label": str(row.get("rating_label") or "").strip(),
        "type": str(row.get("type") or "").strip(),
    }

    bolts = _finite(row.get("bolts"))
    if bolts is not None and bolts >= 1:
        spec["bolts"] = int(round(bolts))
    dn = _finite(row.get("dn"))
    if dn is not None and dn > 0:
        spec["dn"] = dn

    # 内孔直径近似（仅示意，不标注）
    bore = None
    if spec["type"] != "blind":
        if spec["hub_weld"]:
            bore = spec["hub_weld"] * 0.90
        else:
            if spec["dn"]:
                bore = spec["dn"] * 1.05
            elif spec["hub"]:
                bore = spec["hub"] * 0.75
            else:
                bore = od * 0.35
            if spec["hub"]:
                bore = min(bore, spec["hub"] * 0.86)
            if spec["rf"]:
                bore = min(bore, spec["rf"] * 0.92)
    spec["bore"] = bore if (bore and bore > 0) else None
    spec["blind"] = spec["type"] == "blind"
    return spec


# ============================================================
# 9. 几何规划
# ============================================================


def _hole_angles(count: int):
    """螺栓孔角度（数学坐标，逆时针，0° = 右）。

    偶数孔从 180/n 起排，关于水平、竖直中心线都对称；奇数孔让一个孔落在
    正上方。
    """
    if count <= 0:
        return []
    start = 90.0 if count % 2 == 1 else 180.0 / count
    return [start + 360.0 * i / count for i in range(count)]


def _plan(spec: dict, lab: dict, th: dict) -> dict:
    """计算两个视图的全部几何量与文字位置。"""
    fs = th["fs_dim"]
    gap = th["exit_gap"]
    geo = {}
    od = spec["od"]
    cy = CY

    # ---------- 文字内容 ----------
    geo["label_od"] = lab["od"].format(spec["od_s"])
    geo["label_rf"] = (lab["rf"].format(spec["rf_s"])
                       if (spec["rf"] and spec["rf_s"]) else None)
    geo["label_k1"] = lab["bc_name"] if spec["bc_s"] else None
    geo["label_k2"] = lab["bc_value"].format(spec["bc_s"]) if spec["bc_s"] else None
    geo["label_hole"] = (lab["hole"].format(spec["bolts"], spec["bolt_hole_s"])
                         if (spec["bolts"] and spec["bolt_hole_s"]) else None)
    geo["label_c"] = lab["th"].format(spec["th_s"]) if spec["th_s"] else None
    geo["label_h"] = (lab["length"].format(spec["length_s"])
                      if (spec["length_s"] and spec["hub"] and spec["length"]
                          and spec["length"] > (spec["rf_height"] or 0)
                          + (spec["th"] or 0) + 0.5) else None)
    geo["label_f"] = (lab["f"].format(spec["rf_height_s"])
                      if (spec["rf_height_s"] and spec["rf"]) else None)
    geo["label_a"] = (lab["hub"].format(spec["hub_s"])
                      if (spec["hub_s"] and geo["label_h"]) else None)
    geo["label_weld"] = (lab["weld"].format(spec["hub_weld_s"])
                         if (spec["hub_weld_s"] and geo["label_h"]) else None)

    # ---------- 端面视图可用半径 ----------
    w_k1 = _text_width(geo["label_k1"], fs) if geo["label_k1"] else 0.0
    w_k2 = _text_width(geo["label_k2"], fs) if geo["label_k2"] else 0.0
    w_k = max(w_k1, w_k2)
    k_right = FRONT_R
    k_left = k_right - w_k
    # 外圆右侧要给 K 文字留出 w_k + 2 的空间，左侧留 FRONT_PAD_L
    avail = (k_left - FRONT_PAD_R) - FRONT_PAD_L
    r_front = min(R_OD_MAX, max(R_OD_MIN, avail / 2.0))
    geo["k_left"] = k_left
    geo["k_right"] = k_right
    geo["w_k"] = w_k

    # ---------- 剖视图可用半径（保证右侧 A / 焊接端文字不出画布） ----------
    hub_ok = (spec["hub"] is not None and spec["length"] is not None
              and spec["length"] > (spec["rf_height"] or 0.0)
              + (spec["th"] or 0.0) + 0.5)
    geo["draw_hub"] = hub_ok
    w_a = _text_width(geo["label_a"], fs) if geo["label_a"] else 0.0
    w_w = _text_width(geo["label_weld"], fs) if geo["label_weld"] else 0.0
    w_c = _text_width(geo["label_c"], fs) if geo["label_c"] else 0.0
    # 右侧需要预留：A / 焊接端引线文字，或 C 的“文字放在尺寸线右侧”
    if hub_ok:
        dims_right = 5.0 + max(w_a, w_w, w_c) + 2.0
    else:
        dims_right = (5.0 + w_c + 2.0) if w_c else 0.0
    geo["dims_right"] = dims_right
    if spec["blind"]:
        body_ratio = ((spec["rf_height"] or 0.0) + (spec["th"] or 0.0)) / od
    elif hub_ok:
        body_ratio = spec["length"] / od
    else:
        body_ratio = ((spec["rf_height"] or 0.0) + (spec["th"] or 0.0)) / od
    if body_ratio <= 0:
        r_side = R_OD_MAX
    else:
        # 左右各留 2.5 mm，右侧还要放 A / 焊接端引线文字
        r_side = (SIDE_W - 5.0 - dims_right) / (2.0 * body_ratio)
    # 上下标注带（标题 + D / C 文字、H 文字 + 底部注释）也要放得下
    fs_note = th["fs_note"]
    r_top = CY - (TITLE_Y + 0.30 * th["fs_title"] + TOP_CLEARANCE
                  + max(3.2, C_GAP) + 1.7 * fs)
    r_bottom = (NOTE_Y - 0.95 * fs_note - BOTTOM_CLEARANCE
                - (4.5 + 1.75 * fs)) - CY
    r_od = max(R_OD_MIN, min(R_OD_MAX, r_front, r_side, r_top, r_bottom))
    geo["r_od"] = r_od
    geo["cx"] = (FRONT_PAD_L + (k_left - FRONT_PAD_R)) / 2.0
    geo["cy"] = cy
    geo["scale"] = 2.0 * r_od / od

    # ---------- 端面视图圆 ----------
    s = geo["scale"]
    geo["r_rf"] = (spec["rf"] / 2.0 * s) if spec["rf"] else None
    geo["r_bc"] = (spec["bc"] / 2.0 * s) if spec["bc"] else None
    geo["r_hole"] = (spec["bolt_hole"] / 2.0 * s) if spec["bolt_hole"] else None
    geo["r_bore"] = (spec["bore"] / 2.0 * s) if spec["bore"] else None
    geo["n"] = spec["bolts"] or 0
    geo["angles"] = _hole_angles(geo["n"])
    geo["hole_centers"] = [
        (geo["cx"] + geo["r_bc"] * math.cos(math.radians(a)),
         cy - geo["r_bc"] * math.sin(math.radians(a)))
        for a in geo["angles"]
    ] if (geo["r_bc"] and geo["n"]) else []

    # ---------- 剖视图几何 ----------
    boss = bool(spec["rf"] and spec["rf_height"])
    geo["boss"] = boss
    geo["f_s"] = (spec["rf_height"] * s) if boss else 0.0
    th_s = (spec["th"] * s) if spec["th"] else (od * 0.06 * s)
    geo["th_s"] = th_s
    disc_front_off = geo["f_s"]
    disc_back_off = disc_front_off + th_s
    if hub_ok:
        length_off = spec["length"] * s
        body_off = length_off
    else:
        length_off = None
        body_off = disc_back_off
    geo["body_off"] = body_off
    geo["length_off"] = length_off
    content = body_off + dims_right
    x0 = SIDE_L + max(2.5, (SIDE_W - content) / 2.0)
    geo["x0"] = x0
    geo["disc_front_x"] = x0 + disc_front_off
    geo["disc_back_x"] = x0 + disc_back_off
    geo["body_end_x"] = x0 + body_off
    geo["hub_end_x"] = (x0 + length_off) if hub_ok else None
    if hub_ok:
        geo["r_hub"] = spec["hub"] / 2.0 * s
        weld = spec["hub_weld"]
        if weld is not None:
            geo["r_weld"] = min(weld / 2.0 * s, geo["r_hub"])
            geo["r_hub_end"] = geo["r_weld"]
        else:
            geo["r_weld"] = None
            geo["r_hub_end"] = geo["r_hub"]
    else:
        geo["r_hub"] = geo["r_weld"] = geo["r_hub_end"] = None
    geo["r_face_low"] = geo["r_rf"] if boss else r_od

    # ---------- 纵向基准 ----------
    geo["top"] = cy - r_od
    geo["bottom"] = cy + r_od
    geo["d_line"] = geo["top"] - 3.2
    geo["d_text_y"] = geo["d_line"] - 0.75 * fs
    geo["c_line"] = geo["top"] - 4.8
    geo["c_text_y"] = geo["c_line"] - 0.75 * fs
    # f 放在 C 尺寸线上方一行（左上方），引线指向密封面凸台上部轮廓角点；
    # 不和 C 文字同高，也不穿过 C 的尺寸线 / 尺寸界线。
    geo["f_text_y"] = geo["c_line"] - 0.55 * fs
    geo["h_line"] = geo["bottom"] + 4.5
    geo["h_text_y"] = geo["h_line"] + 1.45 * fs
    geo["bottom_text_y"] = geo["bottom"] + BOTTOM_TEXT_GAP
    # K 文字块：整体下移到 cy+20 附近（该高度外圆右缘明显内收），
    # 保证文字与外圆轮廓、螺栓孔、D 尺寸线都不相交；
    # 极小规格再上移，避开下方孔标注文字。
    geo["k_line1_y"] = min(cy + 20.0,
                           geo["bottom_text_y"] - 2.9 * fs - 0.9)
    geo["k_line2_y"] = geo["k_line1_y"] + 1.55 * fs
    return geo


# ============================================================
# 10. 端面视图
# ============================================================


def _front_avoid(geo, spec, th, *, include_k=True, include_hole=True):
    """端面视图中引线需要避让的圆 / 线段 / 文字框。"""
    fs = th["fs_dim"]
    cx, cy, r = geo["cx"], geo["cy"], geo["r_od"]
    circles = []
    if geo["r_hole"]:
        for hx, hy in geo["hole_centers"]:
            circles.append(((hx, hy), geo["r_hole"] + 0.5))
    segments = [
        ((cx - r - 2.5, cy), (cx + r + 2.5, cy), 0.35),   # 水平中心线
        ((cx, cy - r - 2.5), (cx, cy + r + 2.5), 0.35),   # 竖直中心线
        ((cx - r, geo["d_line"]), (cx + r, geo["d_line"]), 0.35),  # D 尺寸线
        ((cx - r, cy), (cx - r, geo["d_line"] - 0.45 * fs), 0.35),  # 左界线
        ((cx + r, cy), (cx + r, geo["d_line"] - 0.45 * fs), 0.35),  # 右界线
    ]
    rects = []
    if geo["label_od"]:
        w = _text_width(geo["label_od"], fs)
        rects.append(_rect(cx, geo["d_text_y"], w, "middle", fs))
    if include_k and geo["w_k"]:
        rects.append((geo["k_left"], geo["k_line1_y"] - 0.95 * fs,
                      geo["k_right"], geo["k_line2_y"] + 0.30 * fs))
    if geo["label_rf"]:
        w = _text_width(geo["label_rf"], fs)
        rects.append(_rect(FRONT_L, geo["bottom_text_y"], w, "start", fs))
    if include_hole and geo["label_hole"]:
        w = _text_width(geo["label_hole"], fs)
        rects.append(_rect(FRONT_R - 2.0, geo["bottom_text_y"], w, "end", fs))
    return {"circles": circles, "segments": segments, "rects": rects}


def _draw_front(parts, spec, geo, lab, th):
    fs = th["fs_dim"]
    cx, cy, r = geo["cx"], geo["cy"], geo["r_od"]

    # ---- 圆与孔 ----
    parts.append(_circle(cx, cy, r, w=th["lw_outline"]))
    if geo["r_rf"]:
        parts.append(_circle(cx, cy, geo["r_rf"], w=th["lw_inner"]))
    if geo["r_bore"]:
        parts.append(_circle(cx, cy, geo["r_bore"], w=th["lw_inner"]))
    if geo["r_bc"]:
        parts.append(_circle(cx, cy, geo["r_bc"], w=th["lw_thin"],
                             dash=DASH_CENTER))
    if geo["r_hole"]:
        for hx, hy in geo["hole_centers"]:
            parts.append(_circle(hx, hy, geo["r_hole"], w=th["lw_inner"],
                                 fill="#ffffff"))

    # ---- 中心线 ----
    parts.append(_line(cx - r - 2.5, cy, cx + r + 2.5, cy,
                       w=th["lw_thin"], dash=DASH_CENTER))
    parts.append(_line(cx, cy - r - 2.5, cx, cy + r + 2.5,
                       w=th["lw_thin"], dash=DASH_CENTER))

    # ---- D：上方线性尺寸 ----
    _dim_h(parts, th, cx - r, cx + r, geo["d_line"], geo["label_od"],
           ext_ys=(cy, cy), text_side="above", text_x=cx)

    # 下方文字行的横向可用空间（避免与 D 尺寸线/其他文字冲突）
    avoid = _front_avoid(geo, spec, th)

    # ---- K：右侧两行引线文字 ----
    k_leader = None
    if geo["w_k"] and geo["label_k2"]:
        x_text = geo["k_right"]
        k_leader = _draw_leader(parts, th, (x_text, geo["k_line1_y"]),
                                x_text, geo["k_line1_y"], geo["label_k1"],
                                "end", avoid, 315.0,
                                radius_center=(cx, cy, geo["r_bc"]))
        parts.append(_text(x_text, geo["k_line2_y"], geo["label_k2"],
                           size=fs, anchor="end"))
    if k_leader is not None:
        # 后面 n×Ød 的引线要避让 K 的引线，避免两条引线在右下角交叉
        avoid["segments"].append((k_leader[0], k_leader[1], 0.5))

    # ---- n×Ød：右下方引线，指向一个孔 ----
    if geo["label_hole"] and geo["hole_centers"]:
        x_text = FRONT_R - 2.0
        y_text = geo["bottom_text_y"]
        width = _text_width(geo["label_hole"], fs)
        target = _leader_target(x_text, y_text, width, "end", fs)
        best = None
        for (hx, hy), ang in zip(geo["hole_centers"], geo["angles"]):
            if math.sin(math.radians(ang)) > -0.15:
                continue  # 只考虑下半圈的孔
            dx, dy = target[0] - hx, target[1] - hy
            dist = math.hypot(dx, dy)
            if dist < 1e-6:
                continue
            tip = (hx + dx / dist * geo["r_hole"],
                   hy + dy / dist * geo["r_hole"])
            local = {
                "circles": [c for c in avoid["circles"]
                            if math.hypot(c[0][0] - hx, c[0][1] - hy) > 1e-6],
                "segments": list(avoid["segments"]),
                "rects": list(avoid["rects"]),
            }
            bad = _check_leader(target, tip, local, th)
            score = bad * 1000.0 + _ang_dist(ang, 300.0) * 0.6
            if best is None or score < best[0]:
                best = (score, tip)
        if best is not None:
            _draw_leader(parts, th, best[1], x_text, y_text,
                         geo["label_hole"], "end", avoid, 300.0)

    # ---- RF：左下引线 ----
    if geo["label_rf"]:
        avoid_rf = _front_avoid(geo, spec, th, include_hole=False)
        _draw_leader(parts, th, (FRONT_L, geo["bottom_text_y"]),
                     FRONT_L, geo["bottom_text_y"], geo["label_rf"],
                     "start", avoid_rf, 225.0,
                     radius_center=(cx, cy, geo["r_rf"]))


# ============================================================
# 11. 剖视（侧视）视图
# ============================================================


def _draw_section(parts, spec, geo, lab, th, hatch_id):
    fs = th["fs_dim"]
    cy = geo["cy"]
    r_od = geo["r_od"]
    r_bore = geo["r_bore"]
    r_rf = geo["r_rf"]
    boss = geo["boss"]
    x0 = geo["x0"]
    disc_front = geo["disc_front_x"]
    disc_back = geo["disc_back_x"]
    body_end = geo["body_end_x"]
    draw_hub = geo["draw_hub"]
    top = geo["top"]
    bottom = geo["bottom"]

    # ---------- 剖切半边的实体轮廓（上半 / 下半） ----------
    def upper_outline():
        pts = []
        if boss and r_rf:
            pts.append((x0, cy - r_rf))
            pts.append((disc_front, cy - r_rf))
        else:
            pts.append((x0, cy - r_od))
        pts.append((disc_front, cy - r_od))
        pts.append((disc_back, cy - r_od))
        if draw_hub:
            pts.append((disc_back, cy - geo["r_hub"]))
            pts.append((geo["hub_end_x"], cy - geo["r_hub_end"]))
        return pts

    outer = upper_outline()
    if r_bore:
        upper = [(x0, cy - r_bore)] + outer + [(geo["hub_end_x"] if draw_hub
                                                else disc_back, cy - r_bore)]
    else:
        upper = outer

    def mirror(points):
        return [(x, 2.0 * cy - y) for x, y in points]

    if spec["blind"]:
        # 盲板：整块实心
        pts = []
        if boss and r_rf:
            pts += [(x0, cy - r_rf), (disc_front, cy - r_rf)]
        else:
            pts.append((x0, cy - r_od))
        pts += [(disc_front, cy - r_od), (disc_back, cy - r_od),
                (disc_back, cy + r_od), (disc_front, cy + r_od)]
        if boss and r_rf:
            pts += [(disc_front, cy + r_rf), (x0, cy + r_rf)]
        else:
            pts.append((x0, cy + r_od))
        parts.append(_poly_path(pts, close=True, fill=f"url(#{hatch_id})",
                                w=th["lw_outline"]))
    else:
        # 半剖：上半剖切（剖面线 + 内孔），下半只画外轮廓。
        # 这样零件外轮廓连续，不会看成上下两块；内孔只在剖切的上半表示。
        parts.append(_poly_path(upper, close=True,
                                fill=f"url(#{hatch_id})",
                                w=th["lw_outline"]))
        end_x = geo["hub_end_x"] if draw_hub else disc_back
        # 下半个轮廓从内孔边缘起、到内孔边缘止，避免在轴线→外径之间
        # 画出一条穿过内孔区域的竖线；外轮廓仍包含轮毂锥面。
        r_edge = r_bore if r_bore else r_od
        lower = [(x0, cy + r_edge)] + mirror(outer) + [(end_x, cy + r_edge)]
        parts.append(_poly_path(lower, close=False, fill="none",
                                w=th["lw_outline"]))
        if r_bore:
            # 下半为外视图：内孔用虚线（不可见轮廓）示意，和上半剖切对应
            parts.append(_line(x0, cy + r_bore, end_x, cy + r_bore,
                               w=th["lw_hidden"], dash=DASH_HIDDEN))

    # ---------- 螺栓孔（虚线示意，内外半都画） ----------
    if geo["r_bc"] and geo["r_hole"]:
        bc_r = geo["r_bc"]
        hole_r = geo["r_hole"]
        for sign in (-1.0, 1.0):
            center = cy + sign * bc_r
            y1 = center - hole_r
            y2 = center + hole_r
            for yy in (y1, y2):
                parts.append(_line(disc_front, yy, disc_back, yy,
                                   w=th["lw_hidden"], dash=DASH_HIDDEN))
            parts.append(_line(disc_front, y1, disc_front, y2,
                               w=th["lw_hidden"], dash=DASH_HIDDEN))
            parts.append(_line(disc_back, y1, disc_back, y2,
                               w=th["lw_hidden"], dash=DASH_HIDDEN))
            parts.append(_line(disc_front - 1.2, center, disc_back + 1.2,
                               center, w=th["lw_thin"], dash=DASH_CENTER))

    # ---------- 轴线（只在轴线处画点划线） ----------
    parts.append(_line(x0 - 3.5, cy, body_end + 3.5, cy,
                       w=th["lw_thin"], dash=DASH_CENTER))

    # ---------- C：上方尺寸（文字放在尺寸线右侧） ----------
    if geo["label_c"]:
        _dim_h(parts, th, disc_front, disc_back, geo["c_line"],
               geo["label_c"], ext_ys=(top, top), text_side="right")

    # ---------- f：左上引线（指向密封面凸台上部轮廓角点） ----------
    if geo["label_f"] and boss and r_rf:
        feature = (x0, cy - r_rf)
        text_x = x0 - 2.5
        _draw_leader(parts, th, feature, text_x, geo["f_text_y"],
                     geo["label_f"], "end", {"circles": [], "segments": [],
                                             "rects": []}, 0.0)

    # ---------- H：下方尺寸 ----------
    if geo["label_h"] and draw_hub:
        _dim_h(parts, th, x0, geo["hub_end_x"], geo["h_line"],
               geo["label_h"],
               ext_ys=(cy + geo["r_face_low"], cy + geo["r_hub_end"]),
               text_side="below")

    # ---------- A / 焊接端：右侧引线（不同高度） ----------
    avoid = {"circles": [], "segments": [], "rects": []}
    if geo["label_a"] and draw_hub:
        feature = (disc_back, cy - geo["r_hub"])
        text_y = cy - geo["r_hub"] + 0.35 * fs
        _draw_leader(parts, th, feature, body_end + 5.0, text_y,
                     geo["label_a"], "start", avoid, 0.0)
    if geo["label_weld"] and draw_hub and geo["r_weld"]:
        feature = (geo["hub_end_x"], cy + geo["r_weld"])
        text_y = cy + geo["r_weld"] + 0.35 * fs
        _draw_leader(parts, th, feature, body_end + 5.0, text_y,
                     geo["label_weld"], "start", avoid, 0.0)

    # ---------- 无轮毂数据时的说明（利用右侧空白） ----------
    if not spec["blind"]:
        note = None
        if geo["label_h"] is None:
            if spec["hub"] is None:
                note = lab["note_plate"] if spec["type"] == "slip-on" else lab["note_nohub"]
            else:
                note = lab["note_nolen"]
        if note:
            size = th["fs_note"]
            parts.append(_text(SIDE_R - 2.0, geo["bottom_text_y"], note,
                               size=size, anchor="end"))


# ============================================================
# 12. 图头 / 图脚
# ============================================================


def _make_title(spec, lab) -> str:
    type_names = lab["type_names"]
    type_name = type_names.get(spec["type"], spec["type"])
    size = spec["size_label"] or spec["size"]
    if spec["dn"]:
        dn_text = f"DN{int(spec['dn'])}"
        if size.upper().startswith("DN"):
            size_part = size
        elif size:
            size_part = f"{size} ({dn_text})"
        else:
            size_part = dn_text
    else:
        size_part = size
    std_name = STD_NAMES.get(spec["std"],
                             spec["std"].upper() if spec["std"] else "")
    chunks = [c for c in (std_name, size_part, spec["rating_label"], type_name)
              if c]
    return " · ".join(chunks)


def _scale_text(scale: float) -> str:
    if not scale or not math.isfinite(scale) or scale <= 0:
        return "-"
    ratio = 1.0 / scale
    if ratio >= 10.0:
        return f"1:{ratio:.0f}"
    return f"1:{ratio:.1f}"


# ============================================================
# 13. 主函数
# ============================================================


def flange_svg(row: dict, *, lang: str = "zh",
               width_mm: float = 170.0, height_mm: float = 105.0) -> str:
    """根据一行法兰尺寸数据生成尺寸标注 SVG。

    参数
    ----
    row : dict
        尺寸行（``flange_dimension`` / ``dimensions/*.json`` 里的一条）。
    lang : {"zh", "en"}
        标注语言，默认中文。
    width_mm, height_mm : float
        画布尺寸（mm），默认 170×105；打印侧栏可传 116×74。两个视图共用
        同一比例尺，字号会随画布大小自动补偿。

    返回
    ----
    str
        完整 ``<svg ...>...</svg>``；``od`` 缺失或非法时返回带提示文字的
        空图（仍是合法 SVG），不抛异常。
    """
    lab = _labels(lang)
    canvas_w = _canvas(width_mm, DESIGN_W)
    canvas_h = _canvas(height_mm, DESIGN_H)
    th = _theme(canvas_w, canvas_h)

    spec = _read_spec(row)
    if spec is None:
        return _empty_svg(canvas_w, canvas_h, lab, th)

    geo = _plan(spec, lab, th)
    title = _make_title(spec, lab)
    hatch_id = _hatch_id(spec)

    parts = []
    # 标题
    size = th["fs_title"]
    max_width = DESIGN_W - 2.0 * CANVAS_L
    width = _text_width(title, size)
    if width > max_width:
        size = max(th["fs_dim"], size * max_width / width)
    parts.append(_text(DESIGN_W / 2.0, TITLE_Y, title, size=size,
                       weight="600"))
    # 视图名称（端面 / 剖视）默认不在图内重复，避免与标题、C 文字争抢空间

    _draw_front(parts, spec, geo, lab, th)
    _draw_section(parts, spec, geo, lab, th, hatch_id)

    # 底部：比例 / 单位
    ratio_text = _scale_text(geo["scale"])
    parts.append(_text(CANVAS_L, NOTE_Y, lab["scale_note"].format(ratio_text),
                       size=th["fs_note"], anchor="start"))
    parts.append(_text(CANVAS_R, NOTE_Y, lab["unit"],
                       size=th["fs_note"], anchor="end"))

    step = th["hatch_step"]
    defs = (
        "<defs>"
        f'<pattern id="{hatch_id}" width="{step:.2f}" height="{step:.2f}" '
        'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        f'<line x1="0" y1="0" x2="0" y2="{step:.2f}" stroke="#000" '
        f'stroke-width="{th["lw_hatch"]:.2f}"/>'
        "</pattern>"
        "</defs>"
    )

    paper = th["paper"]
    content_w = DESIGN_W * paper
    content_h = DESIGN_H * paper
    dx = (canvas_w - content_w) / 2.0
    dy = (canvas_h - content_h) / 2.0
    transform = f"translate({_num(dx)} {_num(dy)}) scale({paper:.4f})"

    inner = "\n".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{canvas_w:.2f}mm" height="{canvas_h:.2f}mm" '
        f'viewBox="0 0 {_num(canvas_w)} {_num(canvas_h)}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'font-family="{FONT_FAMILY}" fill="none" stroke="#000" '
        f'stroke-width="{th["lw_outline"]:.2f}" stroke-linejoin="round">'
        f"<title>{_esc(title)}</title>"
        f'<rect x="0" y="0" width="{_num(canvas_w)}" height="{_num(canvas_h)}" '
        f'fill="#ffffff" stroke="none"/>'
        f"{defs}"
        f'<g transform="{transform}">'
        f"{inner}"
        "</g></svg>"
    )


# ============================================================
# 14. 工具 / 兜底
# ============================================================


def _hatch_id(spec: dict) -> str:
    raw = "-".join(part for part in (
        spec.get("std"), spec.get("type"), spec.get("size"),
    ) if part)
    clean = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in raw)
    clean = clean.strip("-") or "x"
    return f"hatch-{clean}"


def _empty_svg(canvas_w, canvas_h, lab, th) -> str:
    message = lab["empty"]
    size = th["fs_dim"]
    width = _text_width(message, size)
    if width > canvas_w - 10.0:
        size = max(2.8, size * (canvas_w - 10.0) / width)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{canvas_w:.2f}mm" height="{canvas_h:.2f}mm" '
        f'viewBox="0 0 {_num(canvas_w)} {_num(canvas_h)}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'font-family="{FONT_FAMILY}" fill="none" stroke="#000">'
        f"<title>{_esc(message)}</title>"
        f'<rect x="0" y="0" width="{_num(canvas_w)}" height="{_num(canvas_h)}" '
        f'fill="#ffffff" stroke="none"/>'
        f'<text x="{_num(canvas_w / 2.0)}" y="{_num(canvas_h / 2.0)}" '
        f'font-size="{size:.2f}" text-anchor="middle" fill="#000" '
        f'stroke="none">{_esc(message)}</text>'
        "</svg>"
    )
