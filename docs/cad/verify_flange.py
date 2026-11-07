#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法兰重量公式 vs CAD 实体体积 验证器（可复用）
================================================

用途
----
1) 单个规格：按标准库尺寸建 CAD 实体，读体积，和重量引擎的解析公式对比；
2) 批量规格：读 CSV，批量跑，生成 Markdown/JSON/CSV 报告；
3) 可选导出 STEP/STL，供人工看图或后续装配。

用法
----
    # 单件
    python verify_flange.py --std asme-b16-5 --type weld-neck --dn 100 --rating 150

    # 批量
    python verify_flange.py --batch samples.csv --report batch-report.md --json batch-report.json

    # 导出 CAD
    python verify_flange.py --std asme-b16-5 --type blind --dn 100 --rating 150 --export-dir out

约定
----
- 默认 C 不含密封面高度 f（总厚 = C + f）；如果原表 C 已含 f，加 --c-includes-f。
- 对焊内孔 B 默认按 Sch 40 近似；可用 --pipe-id 覆盖单个规格。
- 单位 mm / kg；CAD 体积单位 mm³。
- 通过标准：|公式体积 − CAD 体积| / 公式体积 < 0.1%。

依赖
----
    pip install -r requirements.txt   # cadquery + cadquery-ocp
"""
import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / 'flange-standards-data' / 'data' / 'dimensions'
TYPES = ('weld-neck', 'slip-on', 'blind')
TYPE_ZH = {'weld-neck': '对焊', 'slip-on': '滑套', 'blind': '盲板'}

# 管道外径 / Sch 40 内径（近似，按 DN）
PIPE_OD = {10:17.3,15:21.3,20:26.9,25:33.7,32:42.4,40:48.3,50:60.3,65:76.1,80:88.9,90:101.6,100:114.3,125:139.7,150:168.3,175:193.7,200:219.1,225:244.5,250:273,300:323.9,350:355.6,400:406.4,450:457,500:508,550:559,600:610,650:660,700:711,750:762,800:813,850:864,900:914,950:965,1000:1016,1050:1067,1100:1118,1150:1168,1200:1219,1250:1270,1300:1321,1350:1372,1400:1422,1450:1473,1500:1524}
PIPE_ID = {10:13.0,15:15.8,20:21.0,25:26.6,32:35.1,40:40.9,50:52.5,65:62.7,80:77.9,90:90.1,100:102.3,125:126.6,150:154.1,175:177.8,200:202.7,225:227.6,250:254.5,300:303.2,350:333.3,400:381.0,450:428.7,500:477.8,550:527.2,600:574.6,650:625.0,700:673.0,750:723.9,800:769.9,850:820.0,900:866.4,950:917.0,1000:963.6,1050:1012.0,1100:1062.0,1150:1112.0,1200:1160.0,1250:1208.0,1300:1258.0,1350:1308.0,1400:1356.0,1450:1406.0,1500:1456.0}
DENSITY = 7.85e-6  # kg/mm³, 碳钢
CAL = {
    'weld-neck': {'c':0.976725267, 'p':0.987846019, 'median':8.5, 'p90':21.4},
    'slip-on':   {'c':1.035518675, 'p':1.002767004, 'median':5.8, 'p90':10.6},
    'blind':     {'c':0.976126903, 'p':1.008676824, 'median':3.2, 'p90':8.3},
}


def load_rows():
    rows = []
    for f in sorted(DATA_DIR.glob('*.json')):
        std = f.stem
        for r in json.loads(f.read_text()):
            r = dict(r)
            r['std'] = std
            rows.append(r)
    return rows


def select_row(std, typ, dn=None, rating=None, size=None):
    rows = load_rows()
    out = [r for r in rows if r['std'] == std and r['type'] == typ]
    if dn is not None:
        out = [r for r in out if int(r['dn']) == int(dn)]
    if rating is not None:
        out = [r for r in out if str(r['rating']) == str(rating)]
    if size is not None:
        out = [r for r in out if str(r['size']) == str(size)]
    if not out:
        raise SystemExit(f'未找到规格：{std} / {typ} / dn={dn} rating={rating} size={size}')
    if len(out) > 1 and size is None:
        out = sorted(out, key=lambda r: str(r['size']))
    return out[0]


def pipe_dims(row):
    dn = int(row['dn'])
    od = float(row.get('hub_weld') or PIPE_OD.get(dn) or dn * 1.05)
    pid = PIPE_ID.get(dn) or od * 0.92
    return od, pid


def formula(row, c_includes_f=False, pipe_id=None):
    t = row['type']
    D, C = float(row['od']), float(row['th'])
    RF, f = float(row.get('rf') or 0), float(row.get('rf_height') or 0)
    K, n, dh = float(row.get('bc') or 0), int(row.get('bolts') or 0), float(row.get('bolt_hole') or 0)
    pipe_od, pipe_id = pipe_dims(row)
    if pipe_id is not None:
        pipe_id = float(pipe_id)
    A = row.get('hub'); H = row.get('length'); dw = row.get('hub_weld')
    L = max(0.0, float(H) - C) if H else 0.0
    rf_term = 0.0 if c_includes_f else f
    parts = {'body':0.0, 'hub':0.0, 'rf':0.0, 'hole':0.0}
    if t == 'blind':
        parts['body'] = math.pi/4*D*D*C
        parts['rf'] = math.pi/4*RF*RF*rf_term
        parts['hole'] = n*math.pi/4*dh*dh*C
        V = parts['body'] + parts['rf'] - parts['hole']
    elif t == 'slip-on':
        parts['body'] = math.pi/4*(D*D - pipe_od*pipe_od)*C
        if A and H and L > 0:
            parts['hub'] = math.pi/4*(float(A)**2 - pipe_od*pipe_od)*L
        parts['rf'] = math.pi/4*(RF*RF - pipe_od*pipe_od)*rf_term
        parts['hole'] = n*math.pi/4*dh*dh*C
        V = parts['body'] + parts['hub'] + parts['rf'] - parts['hole']
    else:
        if not A:
            A = D*0.66
        if not dw:
            dw = pipe_od
        if not H:
            H = C + 0.8*pipe_od
            L = max(0.0, float(H) - C)
        A, dw, H = float(A), float(dw), float(H)
        L = max(0.0, H - C)
        parts['body'] = math.pi/4*(D*D - pipe_id*pipe_id)*C
        parts['hub'] = math.pi/12*L*(A*A + A*dw + dw*dw) - math.pi/4*pipe_id*pipe_id*L
        parts['rf'] = math.pi/4*(RF*RF - pipe_id*pipe_id)*rf_term
        parts['hole'] = n*math.pi/4*dh*dh*C
        V = parts['body'] + parts['hub'] + parts['rf'] - parts['hole']
    if V <= 0:
        raise ValueError('公式体积 <= 0，请检查尺寸')
    raw = V * DENSITY
    return {'volume': V, 'raw_weight': raw, 'parts': parts,
            'pipe_od': pipe_od, 'pipe_id': pipe_id, 'c_includes_f': c_includes_f}


def _ring(cq, D, bore, h, offset=0.0):
    wp = cq.Workplane('XY').circle(D/2)
    if bore and bore > 0:
        wp = wp.circle(bore/2)
    return wp.extrude(h).translate((0,0,offset))


def _disc(cq, D, h, offset=0.0):
    return cq.Workplane('XY').circle(D/2).extrude(h).translate((0,0,offset))


def _cyl(cq, D, h, offset=0.0):
    return cq.Workplane('XY').circle(D/2).extrude(h).translate((0,0,offset))


def build_cad(row, c_includes_f=False, pipe_id=None):
    import cadquery as cq
    t = row['type']
    D, C = float(row['od']), float(row['th'])
    RF, f = float(row.get('rf') or 0), float(row.get('rf_height') or 0)
    K, n, dh = float(row.get('bc') or 0), int(row.get('bolts') or 0), float(row.get('bolt_hole') or 0)
    pipe_od, pipe_id0 = pipe_dims(row)
    if pipe_id is not None:
        pipe_id0 = float(pipe_id)
    A = row.get('hub'); H = row.get('length'); dw = row.get('hub_weld')
    rf_h = 0.0 if c_includes_f else f
    if t == 'blind':
        solid = _disc(cq, D, C, 0)
        if rf_h > 0:
            solid = solid.union(_disc(cq, RF, rf_h, -rf_h))
    elif t == 'slip-on':
        solid = _ring(cq, D, pipe_od, C, 0)
        if A and H:
            L = max(0.0, float(H) - C)
            if L > 0:
                solid = solid.union(_ring(cq, float(A), pipe_od, L, C))
        if rf_h > 0:
            solid = solid.union(_ring(cq, RF, pipe_od, rf_h, -rf_h))
    else:
        if not A:
            A = D*0.66
        if not dw:
            dw = pipe_od
        if not H:
            H = C + 0.8*pipe_od
        A, dw, H = float(A), float(dw), float(H)
        L = max(0.0, H - C)
        body = _ring(cq, D, pipe_id0, C, 0)
        hub = cq.Workplane('XY').circle(A/2).workplane(offset=L).circle(dw/2).loft().translate((0,0,C))
        solid = body.union(hub)
        if rf_h > 0:
            solid = solid.union(_ring(cq, RF, pipe_id0, rf_h, -rf_h))
        solid = solid.cut(_cyl(cq, pipe_id0, H + rf_h, -rf_h))
    # 螺栓孔（穿过法兰本体 C）
    holes = None
    for i in range(n):
        ang = 2*math.pi*i/n
        x, y = K/2*math.cos(ang), K/2*math.sin(ang)
        cyl = cq.Workplane('XY').center(x, y).circle(dh/2).extrude(C)
        holes = cyl if holes is None else holes.union(cyl)
    if holes is not None:
        solid = solid.cut(holes)
    return solid


def verify_one(row, c_includes_f=False, pipe_id=None, export_dir=None):
    f = formula(row, c_includes_f, pipe_id)
    solid = build_cad(row, c_includes_f, pipe_id)
    cad_v = float(solid.val().Volume())
    cad_w = cad_v * DENSITY
    err = (cad_v - f['volume']) / f['volume'] * 100 if f['volume'] else 0
    cal = CAL[row['type']]
    cal_w = cal['c'] * (f['raw_weight'] ** cal['p'])
    std_w = row.get('weight')
    out = {
        'std': row['std'], 'type': row['type'], 'dn': int(row['dn']),
        'rating': row['rating'], 'size': row.get('size_label') or row.get('size'),
        'c_includes_f': bool(c_includes_f),
        'formula_v': f['volume'], 'cad_v': cad_v, 'error_pct': err,
        'formula_w': f['raw_weight'], 'cad_w': cad_w,
        'std_w': std_w, 'cal_w': cal_w, 'status': 'PASS' if abs(err) < 0.1 else 'FAIL',
    }
    if export_dir:
        os.makedirs(export_dir, exist_ok=True)
        name = f"{row['std']}_{row['type']}_dn{row['dn']}_{row['rating']}"
        try:
            from cadquery import exporters
            exporters.export(solid, os.path.join(export_dir, name + '.step'))
            exporters.export(solid, os.path.join(export_dir, name + '.stl'))
        except Exception as e:
            out['export_error'] = str(e)
    return out


def read_batch(path):
    with open(path, newline='', encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))


def fmt(v, d=3):
    return f'{v:,.{d}f}'


def write_reports(results, report_path=None, json_path=None, csv_path=None):
    if json_path:
        Path(json_path).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    if csv_path:
        fields = ['std','type','dn','rating','size','formula_v','cad_v','error_pct','formula_w','cad_w','std_w','cal_w','status']
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in results:
                w.writerow({k:r.get(k) for k in fields})
    if report_path:
        lines = ['# 法兰重量公式 vs CAD 体积验证报告', '',
                 f'- 生成时间：{__import__("datetime").datetime.now().isoformat(timespec="seconds")}',
                 f'- 样本数：{len(results)}',
                 f'- 通过：{sum(1 for r in results if r["status"]=="PASS")} / {len(results)}',
                 '- 判定标准：体积误差 < 0.1%',
                 '- 口径：C 不含 f（除非行内标记）；对焊内孔按 Sch 40；RF、直锥台轮毂、螺栓孔。',
                 '', '| 标准 | 类型 | DN | 等级 | 公式体积 mm³ | CAD 体积 mm³ | 误差 % | 公式净重 kg | CAD 净重 kg | 标准表 kg | 校准后 kg | 结论 |',
                 '|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|']
        for r in results:
            lines.append('| {std} | {type} | {dn} | {rating} | {fv} | {cv} | {err:.6f} | {fw} | {cw} | {sw} | {calw} | {status} |'.format(
                std=r['std'], type=r['type'], dn=r['dn'], rating=r['rating'],
                fv=fmt(r['formula_v']), cv=fmt(r['cad_v']), err=r['error_pct'],
                fw=fmt(r['formula_w']), cw=fmt(r['cad_w']),
                sw='—' if r['std_w'] is None else fmt(r['std_w']), calw=fmt(r['cal_w']), status=r['status']))
        Path(report_path).write_text('\n'.join(lines)+'\n', encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description='法兰重量公式 vs CAD 体积验证')
    ap.add_argument('--std')
    ap.add_argument('--type', choices=TYPES)
    ap.add_argument('--dn', type=int)
    ap.add_argument('--rating')
    ap.add_argument('--size')
    ap.add_argument('--batch', help='批量 CSV：列 std,type,dn,rating[,c_includes_f][,export]')
    ap.add_argument('--c-includes-f', action='store_true', help='默认 C 不含 f；加此参数表示 C 已含 f')
    ap.add_argument('--pipe-id', type=float, help='覆盖对焊内孔 B（mm）')
    ap.add_argument('--export-dir', help='导出 STEP/STL 的目录')
    ap.add_argument('--report')
    ap.add_argument('--json')
    ap.add_argument('--csv')
    args = ap.parse_args()

    if args.batch:
        rows = read_batch(args.batch)
        results = []
        for r in rows:
            std = r['std']; typ = r['type']
            if typ not in TYPES:
                raise SystemExit(f'不支持类型：{typ}')
            row = select_row(std, typ, dn=r.get('dn'), rating=r.get('rating'), size=r.get('size') or None)
            c_inc = str(r.get('c_includes_f','0')).strip().lower() in ('1','true','yes','y')
            exp = str(r.get('export','0')).strip().lower() in ('1','true','yes','y')
            res = verify_one(row, c_inc, args.pipe_id, args.export_dir if exp else None)
            results.append(res)
            print(f"{std:14s} {typ:10s} DN{res['dn']:<4} {str(res['rating']):5s} "
                  f"V={fmt(res['formula_v'])} / {fmt(res['cad_v'])} mm³  "
                  f"err={res['error_pct']:.2e}%  {res['status']}")
        write_reports(results, args.report, args.json, args.csv)
        bad = [r for r in results if r['status'] != 'PASS']
        if bad:
            print(f'FAIL: {len(bad)} / {len(results)} 个样本超出 0.1%', file=sys.stderr)
            sys.exit(1)
        print(f'OK: {len(results)} / {len(results)} 通过')
        return

    if not (args.std and args.type and (args.dn or args.size)):
        ap.error('单件模式需要 --std --type 以及 --dn 或 --size')
    row = select_row(args.std, args.type, dn=args.dn, rating=args.rating, size=args.size)
    res = verify_one(row, args.c_includes_f, args.pipe_id, args.export_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if args.report or args.json or args.csv:
        write_reports([res], args.report, args.json, args.csv)
    if res['status'] != 'PASS':
        sys.exit(1)


if __name__ == '__main__':
    main()
