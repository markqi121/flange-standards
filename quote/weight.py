"""法兰重量引擎（w0.3）：前端 JS 原型 → Python 的忠实移植。

规则来源
--------
- 前端原型：``docs/法兰报价系统设计方案.html`` 中的 ``weightFor/volumeParts``（WEIGHT_RULE = ``w0.3``）；
- 校准系数：``docs/cad/calibration.json``（由 971 条参考重量按类型拟合），
  必须与 ``docs/cad/verify_flange.py`` 的 ``CAL`` 保持一致；
- 几何公式已用 CadQuery 独立验证（``docs/重量-CAD验证-4英寸WN.md``，
  4″ Class 150 WN 公式体积 909987.747 mm³，与 CAD 误差 1.3e-14%）。

口径
----
- 尺寸单位 mm，密度 g/cm³，重量 kg；
- 有 ``DimensionRow.weight``（数据包参考重量）时：按密度修正 ``weight × ρ/7.85``，
  source=``standard``、confidence=``B``；
- 无参考重量时：几何体积 × 密度 → 校准模型 ``c × raw^p``，
  source=``geometry``、confidence=``C``；
- 体积分解：环/本体、轮毂、密封面，扣除内孔、螺栓孔；
- 对焊内孔按 Sch 40 近似（与前端/CAD 验证脚本一致），可用 ``pipe_id`` 覆盖。

注意：CAL 系数只用于吸收“参考重量近似/圆角/余量”，不是用来掩盖公式错误；
参考重量本身是数据包近似值，不是标准强制值（见 docs/重量数据来源说明.md）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional

WEIGHT_RULE = 'w0.3'
STEM_DENSITY = Decimal('7.85')  # 碳钢基准密度 g/cm³

# ---------------------------------------------------------------- 管道口径近似
# 与 docs/cad/verify_flange.py、前端 PIPE_OD/PIPE_ID 完全一致（DN -> mm）
PIPE_OD = {
    10: 17.3, 15: 21.3, 20: 26.9, 25: 33.7, 32: 42.4, 40: 48.3, 50: 60.3,
    65: 76.1, 80: 88.9, 90: 101.6, 100: 114.3, 125: 139.7, 150: 168.3,
    175: 193.7, 200: 219.1, 225: 244.5, 250: 273, 300: 323.9, 350: 355.6,
    400: 406.4, 450: 457, 500: 508, 550: 559, 600: 610, 650: 660, 700: 711,
    750: 762, 800: 813, 850: 864, 900: 914, 950: 965, 1000: 1016, 1050: 1067,
    1100: 1118, 1150: 1168, 1200: 1219, 1250: 1270, 1300: 1321, 1350: 1372,
    1400: 1422, 1450: 1473, 1500: 1524,
}
PIPE_ID = {
    10: 13.0, 15: 15.8, 20: 21.0, 25: 26.6, 32: 35.1, 40: 40.9, 50: 52.5,
    65: 62.7, 80: 77.9, 90: 90.1, 100: 102.3, 125: 126.6, 150: 154.1,
    175: 177.8, 200: 202.7, 225: 227.6, 250: 254.5, 300: 303.2, 350: 333.3,
    400: 381.0, 450: 428.7, 500: 477.8, 550: 527.2, 600: 574.6, 650: 625.0,
    700: 673.0, 750: 723.9, 800: 769.9, 850: 820.0, 900: 866.4, 950: 917.0,
    1000: 963.6, 1050: 1012.0, 1100: 1062.0, 1150: 1112.0, 1200: 1160.0,
    1250: 1208.0, 1300: 1258.0, 1350: 1308.0, 1400: 1356.0, 1450: 1406.0,
    1500: 1456.0,
}

# ---------------------------------------------------------------- 校准模型（w0.3）
# 来源 docs/cad/calibration.json，勿随意改动；改动必须升 weight_rule 版本。
CALIBRATION = {
    'weld-neck': {'c': 0.976725267, 'p': 0.987846019, 'median': 8.5, 'p90': 21.4},
    'slip-on': {'c': 1.035518675, 'p': 1.002767004, 'median': 5.8, 'p90': 10.6},
    'blind': {'c': 0.976126903, 'p': 1.008676824, 'median': 3.2, 'p90': 8.3},
}

SUPPORTED_TYPES = tuple(CALIBRATION)


class WeightError(ValueError):
    """无法计算重量（尺寸缺失/几何体积非正等）。"""


def _to_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def quantize_weight(value) -> Decimal:
    """重量统一 3 位小数、四舍五入。"""
    return _to_decimal(value).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)


def _num(value, default=0.0) -> float:
    if value is None:
        return float(default)
    return float(value)


@dataclass
class VolumeParts:
    """几何体积分解（mm³）。"""

    ring: float = 0.0      # 法兰本体环（盲板为整圆板）
    hub: float = 0.0       # 轮毂/锥台
    rf: float = 0.0        # 密封面凸台
    bore: float = 0.0      # 内孔扣除
    bolts: float = 0.0     # 螺栓孔扣除

    def as_dict(self) -> Dict[str, float]:
        return {
            'ring': round(self.ring, 3),
            'hub': round(self.hub, 3),
            'rf': round(self.rf, 3),
            'bore': round(self.bore, 3),
            'bolts': round(self.bolts, 3),
        }


@dataclass
class GeometryResult:
    """几何模型结果（校准前后）。"""

    volume_mm3: float
    parts: VolumeParts
    pipe_od: float
    pipe_id: float
    raw_weight: Decimal
    calibrated: Decimal
    calibration: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            'volume_mm3': round(self.volume_mm3, 3),
            'parts': self.parts.as_dict(),
            'pipe_od': round(self.pipe_od, 3),
            'pipe_id': round(self.pipe_id, 3),
            'raw_weight': str(self.raw_weight),
            'calibrated': str(self.calibrated),
            'calibration': dict(self.calibration),
            'rule_version': WEIGHT_RULE,
        }


@dataclass
class WeightResult:
    """报价采用的净重及其来源/置信度/体积明细（契约 §3.1）。"""

    net_weight: Decimal
    source: str            # standard | geometry
    confidence: str        # B | C
    density: Decimal
    rule_version: str
    volume_mm3: Optional[float] = None
    parts: Dict[str, float] = field(default_factory=dict)
    raw_weight: Optional[Decimal] = None
    standard_weight: Optional[Decimal] = None
    pipe_od: Optional[float] = None
    pipe_id: Optional[float] = None
    calibration: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            'net_weight': float(self.net_weight),
            'source': self.source,
            'confidence': self.confidence,
            'density': float(self.density),
            'rule_version': self.rule_version,
            'volume_mm3': None if self.volume_mm3 is None else round(self.volume_mm3, 3),
            'parts': dict(self.parts),
            'raw_weight': None if self.raw_weight is None else float(self.raw_weight),
            'standard_weight': None if self.standard_weight is None else float(self.standard_weight),
            'pipe_od': self.pipe_od,
            'pipe_id': self.pipe_id,
            'calibration': dict(self.calibration),
        }


# ---------------------------------------------------------------- 几何公式
def _v_disc(diameter: float, height: float) -> float:
    return math.pi / 4 * diameter * diameter * height


def _v_ring(outer: float, inner: float, height: float) -> float:
    return math.pi / 4 * (outer * outer - inner * inner) * height


def _v_frustum(d_top: float, d_bottom: float, height: float) -> float:
    return math.pi * height / 12 * (d_top * d_top + d_top * d_bottom + d_bottom * d_bottom)


def _v_holes(count: int, diameter: float, height: float) -> float:
    return count * math.pi / 4 * diameter * diameter * height


def flange_type_code(row) -> str:
    """兼容 catalog.DimensionRow 的 flange_type_id / flange_type 外键。"""
    code = getattr(row, 'flange_type_id', None)
    if not code:
        fk = getattr(row, 'flange_type', None)
        code = getattr(fk, 'slug', None) or getattr(fk, 'pk', None)
    return str(code or '')


def pipe_dims(row) -> tuple:
    """返回 (管外径, Sch40 内径)，与前端 ``pipeDims`` 一致。"""
    dn = int(getattr(row, 'dn') or 0)
    pipe_od = PIPE_OD.get(dn) or dn * 1.05
    pipe_id = PIPE_ID.get(dn) or pipe_od * 0.92
    type_code = flange_type_code(row)
    hub_weld = getattr(row, 'hub_weld', None)
    # 对焊：焊接端外径 = 管外径（前端只对对焊应用 hub_weld）
    if type_code == 'weld-neck' and hub_weld:
        pipe_od = float(hub_weld)
    return pipe_od, pipe_id, dn


def volume_parts(row, c_includes_f: bool = False) -> tuple:
    """计算体积分解，返回 (VolumeParts, 净体积 mm³, pipe_od, pipe_id)。"""
    type_code = flange_type_code(row)
    if type_code not in SUPPORTED_TYPES:
        raise WeightError(f'不支持的法兰类型：{type_code}')

    big_d = _num(getattr(row, 'od', None))
    thickness = _num(getattr(row, 'th', None))
    rf_d = _num(getattr(row, 'rf', None))
    rf_h = 0.0 if c_includes_f else _num(getattr(row, 'rf_height', None))
    bolts = int(getattr(row, 'bolts', None) or 0)
    hole_d = _num(getattr(row, 'bolt_hole', None))
    pipe_od, pipe_id, _dn = pipe_dims(row)

    if big_d <= 0 or thickness <= 0:
        raise WeightError('缺失外径/厚度，无法估算重量')

    parts = VolumeParts()
    if type_code == 'blind':
        parts.ring = _v_disc(big_d, thickness)
        parts.rf = math.pi / 4 * rf_d * rf_d * rf_h
        parts.bolts = _v_holes(bolts, hole_d, thickness)
    elif type_code == 'slip-on':
        parts.ring = _v_ring(big_d, pipe_od, thickness)
        parts.rf = math.pi / 4 * max(0.0, rf_d * rf_d - pipe_od * pipe_od) * rf_h
        parts.bolts = _v_holes(bolts, hole_d, thickness)
        hub_a = getattr(row, 'hub', None)
        length = getattr(row, 'length', None)
        if hub_a and length and _num(length) > thickness:
            parts.hub = _v_ring(_num(hub_a), pipe_od, _num(length) - thickness)
    else:  # weld-neck
        hub_a = _num(getattr(row, 'hub', None)) or big_d * 0.66
        hub_weld = _num(getattr(row, 'hub_weld', None)) or pipe_od
        length = _num(getattr(row, 'length', None)) or thickness + 0.8 * pipe_od
        bore = pipe_id
        hub_len = max(0.0, length - thickness)
        parts.ring = _v_ring(big_d, bore, thickness)
        parts.hub = _v_frustum(hub_a, hub_weld, hub_len)
        parts.bore = math.pi / 4 * bore * bore * hub_len
        parts.rf = math.pi / 4 * max(0.0, rf_d * rf_d - bore * bore) * rf_h
        parts.bolts = _v_holes(bolts, hole_d, thickness)

    volume = parts.ring + parts.hub + parts.rf - parts.bore - parts.bolts
    if not volume > 0:  # 与前端一致：几何异常时退回本体环
        volume = parts.ring
        parts = VolumeParts(ring=parts.ring)
    if not volume > 0:
        raise WeightError('几何体积非正，尺寸数据异常')
    return parts, volume, pipe_od, pipe_id


def calibration_for(flange_type: str) -> Dict[str, float]:
    try:
        return dict(CALIBRATION[flange_type])
    except KeyError:
        raise WeightError(f'缺少 {flange_type} 的校准系数') from None


def apply_calibration(raw_weight: float, flange_type: str) -> float:
    """w0.3 校准模型： c × raw^p。"""
    cal = calibration_for(flange_type)
    raw = float(raw_weight)
    if not math.isfinite(raw) or raw <= 0:
        raise WeightError('理论净重非正，无法校准')
    return cal['c'] * math.pow(raw, cal['p'])


def geometry_weight(row, material, c_includes_f: bool = False) -> GeometryResult:
    """纯几何重量：体积 → 密度 → 校准；source=geometry / confidence=C 的基础。"""
    density = _to_decimal(material.density)
    parts, volume, pipe_od, pipe_id = volume_parts(row, c_includes_f=c_includes_f)
    rho = float(density) * 1e-6  # kg/mm³
    raw = volume * rho
    calibrated = apply_calibration(raw, flange_type_code(row))
    if not math.isfinite(calibrated) or calibrated <= 0:
        raise WeightError('校准后重量非正')
    return GeometryResult(
        volume_mm3=volume,
        parts=parts,
        pipe_od=pipe_od,
        pipe_id=pipe_id,
        raw_weight=quantize_weight(raw),
        calibrated=quantize_weight(calibrated),
        calibration=calibration_for(flange_type_code(row)),
    )


def resolve_weight(row, material, c_includes_f: bool = False) -> WeightResult:
    """报价净重：参考重量优先（B），否则几何校准（C）。

    参数
    ----
    row: ``catalog.DimensionRow``
    material: ``quote.Material``（用 density）
    c_includes_f: 旧表核对开关；标准库数据口径为 C 不含 f，默认 False。
    """
    density = _to_decimal(material.density)
    type_code = flange_type_code(row)

    # 几何始终算一遍：B 级报价单上要显示“几何复核”，C 级直接采用
    try:
        geo = geometry_weight(row, material, c_includes_f=c_includes_f)
    except WeightError:
        geo = None

    reference_weight = getattr(row, 'weight', None)
    if reference_weight is not None:
        reference = _to_decimal(reference_weight)
        net = reference * density / STEM_DENSITY
        return WeightResult(
            net_weight=quantize_weight(net),
            source='standard',
            confidence='B',
            density=density,
            rule_version=WEIGHT_RULE,
            volume_mm3=geo.volume_mm3 if geo else None,
            parts=geo.parts.as_dict() if geo else {},
            raw_weight=geo.raw_weight if geo else None,
            standard_weight=quantize_weight(net),
            pipe_od=geo.pipe_od if geo else None,
            pipe_id=geo.pipe_id if geo else None,
            calibration=geo.calibration if geo else {},
        )

    if geo is None:
        raise WeightError('该规格无参考重量，且几何数据不足，无法估算')
    return WeightResult(
        net_weight=geo.calibrated,
        source='geometry',
        confidence='C',
        density=density,
        rule_version=WEIGHT_RULE,
        volume_mm3=geo.volume_mm3,
        parts=geo.parts.as_dict(),
        raw_weight=geo.raw_weight,
        standard_weight=None,
        pipe_od=geo.pipe_od,
        pipe_id=geo.pipe_id,
        calibration=geo.calibration,
    )
