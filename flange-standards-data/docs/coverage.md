# 数据覆盖范围

> 本文件由 `scripts/build.py` 自动生成，请勿手工编辑。

- 标准数：5
- 尺寸数据行：1348
- 参考文档：4

## 尺寸数据

| 标准 | 版本 | 行数 | 类型 | 压力等级 | 尺寸数 | DN 范围 |
|---|---|---|---|---|---|---|
| ASME B16.5 (`asme-b16-5`) | ASME B16.5-2025 | 382 | weld-neck, slip-on, blind | 150, 300, 400, 600, 900, 1500, 2500 | 21 | 1/2″–24″ (DN15–DN600) |
| ASME B16.47 Series A (`asme-b16-47-a`) | ASME B16.47-2025 | 168 | blind, weld-neck | 150, 300, 400, 600, 900 | 18 | 26″–60″ (DN650–DN1500) |
| ASME B16.47 Series B (`asme-b16-47-b`) | ASME B16.47-2025 | 168 | blind, weld-neck | 150, 300, 400, 600, 900 | 18 | 26″–60″ (DN650–DN1500) |
| JIS B2220 (`jis-b2220`) | JIS B2220:2012 | 330 | weld-neck, slip-on, blind | 5k, 10k, 16k, 20k, 30k | 24 | 10A–600A (DN10–DN600) |
| EN 1092-1 (`en-1092-1`) | EN 1092-1:2018 | 300 | weld-neck, slip-on, blind | pn6, pn10, pn16, pn25, pn40 | 20 | DN10–DN600 |

## 参考表

| 标准 | Topic | 表格数 | 数据行 | 说明 |
|---|---|---|---|---|
| asme-b16-47 | pressure-ratings | 3 | 35 | ASME B16.47-2025 pressure–temperature ratings (Group 1.1) |
| asme-b16-47 | material-groups | 1 | 67 | ASME B16.47-2025 material groups (Table 1) |
| asme-b16-47 | tolerances | 1 | 17 | ASME B16.47-2025 flange tolerances (Section 7) |
| asme-b16-47 | marking | 1 | 11 | ASME B16.47-2025 marking requirements (Section 4) |
| asme-b16-47 | changes | 2 | 22 | ASME B16.47-2025 changes and record numbers |
| asme-b16-5 | pressure-ratings | 16 | 321 | ASME B16.5-2025 pressure ratings by material group |
| asme-b16-5 | material-groups | 5 | 179 | ASME B16.5-2025 material groups and bolting tables |
| asme-b16-5 | tolerances | 3 | 36 | ASME B16.5-2025 flange tolerances (Section 7) |
| asme-b16-5 | marking | 1 | 11 | ASME B16.5-2025 marking requirements (Section 4) |
| asme-b16-5 | changes | 1 | 22 | ASME B16.5-2025: what changed from the 2020 edition |
| en-1092-1 | pressure-ratings | 3 | 39 | EN 1092-1 pressure–temperature ratings |
| en-1092-1 | material-groups | 2 | 26 | EN 1092-1 material groups (1E0 to 16E0) |
| en-1092-1 | tolerances | 7 | 107 | EN 1092-1 flange tolerances (Table 22) |
| en-1092-1 | marking | 1 | 9 | EN 1092-1 flange marking (§5.10) |
| en-1092-1 | types | 4 | 38 | EN 1092-1 flange types and facings |
| jis-b2220 | pressure-ratings | 1 | 59 | JIS B2220 pressure–temperature ratings |
| jis-b2220 | material-groups | 2 | 17 | JIS B2220 material groups and ASTM reference materials |
| jis-b2220 | tolerances | 2 | 45 | JIS B2220 dimensional tolerances (Table 22) |
| jis-b2220 | marking | 2 | 16 | JIS B2220 marking requirements |
| jis-b2220 | types | 2 | 14 | JIS B2220 flange types and gasket faces |

## 覆盖限制（标准本身的规定，不是缺数据）

- ASME B16.5：Class 2500 没有滑套法兰（slip-on）；Class 1500 的滑套法兰只到 NPS 2-1/2；Class 900/1500/2500 没有 NPS 3-1/2。
- ASME B16.47：只定义对焊法兰（weld-neck）和盲板法兰（blind），没有滑套法兰；Class 900 只到 NPS 48；Series B NPS 38 以上按 General Note (h) 采用 Series A 尺寸。
- JIS B2220：30K 只到 400A；16K/20K 不含 175A、225A。
- EN 1092-1：尺寸表覆盖 DN10–DN600、PN6–PN40；PN6 从 DN10 起。

> 尺寸和额定值以购买的标准版本为准；本数据包用于查询和预选型，不能替代标准原文。
