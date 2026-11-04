# 法兰标准数据包 · flange-standards-data

从 Website 项目中抽离出来的**独立法兰标准数据包**，覆盖 ASME、EN、JIS 三个体系共 4 个标准、5 套尺寸数据。
后续的「标准数据查询」功能可以直接读取本目录，不需要 Astro、不需要站点代码、不需要网络。

- **尺寸数据**：1,348 行（一行 = 一个标准 + 法兰类型 + 尺寸 + 压力等级）
- **参考数据**：4 个标准文档、20 个主题、60 张表、1,091 行、7,345 个单元格
- **格式**：规范 JSON、CSV、SQLite 查询库，附 JSON Schema、字段字典、来源记录和校验脚本
- **依赖**：读取数据零依赖；只有「从站点重新生成」才需要 Node/Python 和站点源码

数据范围、每个标准的覆盖限制见 [docs/coverage.md](docs/coverage.md)。

## 目录结构

```
flange-standards-data/
├── data/
│   ├── standards.json          标准元数据（名称、版本、类型、压力等级、材料选项）
│   ├── dimensions/             5 套尺寸数据（JSON，规范源文件）
│   ├── reference/              4 个标准的参考文档（JSON，规范源文件）
│   └── manifest.json           构建产物：行数统计 + 每个文件的 SHA-256
├── csv/
│   ├── dimensions.csv          全部尺寸行，扁平表（含派生英制列）
│   ├── dimension-summary.csv   按标准的汇总
│   ├── reference-index.csv     所有参考表的索引（表号、页码、行列数）
│   └── reference/              按「标准--主题」导出的参考表 CSV（20 个）
├── sqlite/
│   └── flange-standards.sqlite 可直接查询的数据库（尺寸 + 参考表 + 来源）
├── schema/                     JSON Schema（dimensions / reference / standards）
├── docs/
│   ├── fields.md               字段字典、单位、空值规则
│   ├── coverage.md             数据覆盖范围（自动生成）
│   ├── sources.md              标准版本、来源文档、表号/页码（自动生成）
│   ├── query-guide.md          SQL 查询示例（尺寸 / p/T / 材料组 / 跨标准对比）
│   └── legal.md                数据性质、版权边界与免责声明
├── examples/
│   └── queries.sql             可直接执行的示例查询
└── scripts/                    重新生成与校验脚本（维护用）
```

## 快速开始

### 1. SQLite（推荐给查询功能）

```bash
sqlite3 sqlite/flange-standards.sqlite
```

```sql
.headers on
.mode column

-- ASME B16.5，NPS 6，Class 150 的三种法兰类型
SELECT type, od_mm, th_mm, bc_mm, bolts, bolt_hole_mm
FROM flange_dimension
WHERE std = 'asme-b16-5' AND size = '6' AND rating = '150';
```

常用表：

| 表 | 内容 |
|---|---|
| `flange_dimension` | 1,348 行尺寸数据，主查询表 |
| `standard` / `standard_type` / `standard_rating` / `standard_size` | 标准、类型、压力等级、尺寸的枚举与显示名 |
| `reference_topic` | 20 个参考主题（p/T、材料组、公差、标记、变更、类型） |
| `reference_table` / `reference_column` / `reference_cell` | 参考表的表头、列定义与单元格（全部按文本保存） |
| `reference_source` | 每张参考表的来源引用（标准表号 + PDF 页码） |
| `v_reference_cell` | 把主题、表、列定义和单元格拼平的视图，做临时查询最方便 |

### 2. JSON（推荐给程序解析）

```js
import dimensions from './data/dimensions/asme-b16-5.json' with { type: 'json' };

const row = dimensions.find(
  (r) => r.type === 'weld-neck' && r.size === '6' && r.rating === '150',
);
// { od: 279.4, th: 23.9, bc: 241.3, bolts: 8, bolt_hole: 22.2, ... }
```

所有数值字段单位为 **毫米**（`weight` 为 kg），英制值不落库，需要时按 `mm / 25.4` 换算。
`data/standards.json` 提供标准名称、版本、法兰类型显示名、压力等级顺序等元数据。

### 3. CSV（推荐给 Excel / BI）

- `csv/dimensions.csv`：全部尺寸行，UTF-8 BOM，Excel 直接双击打开；
- `csv/reference/*.csv`：每个标准的每个主题一个文件，`#` 开头的是说明/来源行，其后是表头和数据；
- `csv/reference-index.csv`：先看这个文件定位表号和页码。

## 数据模型

### 尺寸数据（一行一规格）

```json
{
  "std": "asme-b16-5",
  "type": "weld-neck",
  "size": "6",
  "size_label": "6″",
  "dn": 150,
  "rating": "150",
  "rating_label": "Class 150",
  "od": 279.4,
  "th": 23.9,
  "rf": 215.9,
  "rf_height": 1.5,
  "bc": 241.3,
  "bolts": 8,
  "bolt_hole": 22.2,
  "hub": 192.0,
  "length": 87.4,
  "hub_weld": 168.4,
  "weight": 10.9
}
```

字段含义、单位、空值规则见 [docs/fields.md](docs/fields.md)。

### 参考数据（标准 → 主题 → 章节 → 表）

`data/reference/*.json` 保留完整的结构和来源信息：`edition`、`verifiedOn`、`sourceDocument`、
每个主题的 `sources`（表号 + 页码）、每张表的 `caption` / `note` / `footnotes`。
SQLite 里同一份数据拆成 `reference_topic` / `reference_table` / `reference_column` /
`reference_cell` 四层，单元格全部按**字符串**存储，保留小数逗号、`—`、脚注标记等原始写法。

## 版本与来源

| 标准 | 版本 | 尺寸数据 | 参考数据 |
|---|---|---|---|
| ASME B16.5 | 2025 | 382 行 | p/T、材料组与螺栓、公差、标记、版本变更 |
| ASME B16.47 | 2025（Series A / B） | 168 + 168 行 | p/T、材料组、公差、标记、版本变更 |
| EN 1092-1 | 2018 | 300 行 | p/T、材料组、公差、标记、类型与密封面 |
| JIS B2220 | 2012 | 330 行 | p/T、材料组、公差、标记、类型与密封面 |

- 每个参考主题都记录了核对日期（`verifiedOn`）和来源表号/页码，见 [docs/sources.md](docs/sources.md)。
- 尺寸数据按标准 SI 表逐单元格转录，ASME 三套同时对照英制表；覆盖限制是标准本身的规定（例如 B16.47 没有滑套法兰），不是缺数据。
- 本数据包**不包含标准 PDF、正文段落、图表**，只包含事实性数据与项目自行撰写的说明文字，见 [docs/legal.md](docs/legal.md)。

## 从站点重新生成

站点 `src/data/standards/` 仍是编辑数据的地方。数据变更后在本目录执行：

```bash
node scripts/sync-from-site.mjs   # 从 ../src/data/standards 复制数据并导出标准元数据
python3 scripts/build.py          # 生成 CSV、SQLite、manifest、coverage、sources
python3 scripts/validate.py       # 校验结构、行数、跨类型螺栓一致性、校验和、SQLite 完整性
```

也可以一条命令：

```bash
npm run rebuild
```

> `sync-from-site.mjs` 需要站点 `node_modules`（esbuild 随 Astro/Vite 安装）。
> `build.py` / `validate.py` 只用 Python 3.8+ 标准库。

## 校验

`python3 scripts/validate.py` 会检查：

- 尺寸字段、枚举、数值范围、重复行、同一尺寸/等级下三种法兰类型的螺栓孔模式是否一致；
- 参考表的主题枚举、表头/行宽、单元格类型、caption 与 sources；
- CSV 行数与 JSON 行数、SQLite 行数是否一致；
- SQLite `integrity_check`、外键、已知规格的抽查值；
- manifest 中每个文件的 SHA-256 是否与磁盘一致。

全部通过时输出 `validate: OK`，退出码 0。

## 给后续查询功能的建议

本数据包按「查询功能需要什么」组织，常见查询可以直接落到 SQL：

| 用户查询 | 数据入口 |
|---|---|
| 某标准、某尺寸、某压力等级的法兰尺寸 | `flange_dimension`（按 `std + type + rating + dn` 走索引） |
| 同 DN 下 ASME / EN / JIS 的尺寸差异 | `flange_dimension` 按 `dn` 跨标准 join |
| 某材料组在某温度下的最大允许压力 | `v_reference_cell`，先按 `reference_table.caption` 选表，再按列序取压力 |
| 材料组包含哪些材料牌号 | `reference_table`（material-groups）+ `reference_cell` |
| 公差、标记、版本变更 | 对应 `reference_topic` |
| 展示来源与核对日期 | `reference_topic.verified_on`、`reference_source`、`manifest.version` |

建议接口保留 `edition`、`verifiedOn`、`source`（表号+页码）三类元信息，页面/接口上显示
「以购买的标准版本为准」的声明，与站点现有口径保持一致。

## 合规声明

本数据包是事实性数据的整理结果，不是标准的替代品，也不含标准正文。使用和再分发前请阅读
[docs/legal.md](docs/legal.md)。尺寸和额定值在用于报价、生产或投标前，必须用项目规定的标准
版本复核。
