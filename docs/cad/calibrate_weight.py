#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 971 条标准表重量重新拟合 w0.x 校准系数。

用法：
    python calibrate_weight.py --json calibration.json --md calibration.md

输出每个类型的 c / p / 中位误差 / 90 分位误差，用于更新
方案 HTML 中的 CAL 常量（或 Django quote/weight.py）。
"""
import argparse, json, math, statistics
from pathlib import Path
from verify_flange import load_rows, formula, CAL, DENSITY

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', default='calibration.json')
    ap.add_argument('--md', default='calibration.md')
    args = ap.parse_args()
    rows = load_rows()
    out = {}
    lines = ['# 重量校准系数', '', '| 类型 | 样本 | c | p | 中位误差 | 90 分位 |', '|---|---:|---:|---:|---:|---:|']
    for typ in ('weld-neck','slip-on','blind'):
        rs = [r for r in rows if r['type'] == typ and r.get('weight')]
        xs = [math.log(formula(r)['raw_weight']) for r in rs]
        ys = [math.log(float(r['weight'])) for r in rs]
        n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
        p = sum((x-mx)*(y-my) for x,y in zip(xs,ys)) / sum((x-mx)**2 for x in xs)
        c = math.exp(my - p*mx)
        errs = sorted(abs(c*(formula(r)['raw_weight'])**p - float(r['weight']))/float(r['weight']) for r in rs)
        med = statistics.median(errs)*100
        p90 = errs[int(n*0.9)]*100
        out[typ] = {'c': round(c,9), 'p': round(p,9), 'n': n,
                    'median': round(med,1), 'p90': round(p90,1)}
        lines.append(f'| {typ} | {n} | {c:.6f} | {p:.6f} | {med:.1f}% | {p90:.1f}% |')
    Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    Path(args.md).write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
