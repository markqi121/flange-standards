# 字段字典

本文件说明 `data/`、`csv/`、`sqlite/` 中每个字段的含义、单位与空值规则。

## 1. 尺寸数据（`data/dimensions/*.json` / `csv/dimensions.csv` / `flange_dimension`）

一行 = 一个标准 + 一种法兰类型 + 一个公称尺寸 + 一个压力等级。
长度为 **毫米**，重量为 **kg**。

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `std` | 枚举 | 否 | 数据来源标准：`asme-b16-5`、`asme-b16-47-a`、`asme-b16-47-b`、`jis-b2220`、`en-1092-1` |
| `type` | 枚举 | 否 | `weld-neck`（对焊）、`slip-on`（滑套）、`blind`（盲板） |
| `size` | 字符串 | 否 | 稳定的 URL 键。ASME：`1-2`、`3-4`、`1-1-4`、`2-1-2`、`6`、`24`；B16.47：`26`…`60`；JIS：`10a`…`600a`；EN：`dn10`…`dn600` |
| `size_label` | 字符串 | 否 | 展示用尺寸：`1/2″`、`2½″`、`26″`、`10A`、`DN100` |
| `dn` | 整数（mm） | 否 | 公称尺寸的毫米等价值，用于跨标准比较。ASME/B16.47 为对应 DN，JIS 为 A 数，EN 为 DN |
| `rating` | 枚举 | 否 | ASME：`150`…`2500`；JIS：`5k`…`30k`；EN：`pn6`…`pn40` |
| `rating_label` | 字符串 | 否 | 展示名：`Class 150`、`10K`、`PN16` |
| `od` | 小数（mm） | 否 | 法兰外径 D |
| `th` | 小数（mm） | 否 | 法兰厚度；盲板为盲板厚度（不含密封面） |
| `rf` | 小数（mm） | 是 | 密封面（RF）直径。标准表未定义该类型/尺寸时为 `null` |
| `rf_height` | 小数（mm） | 是 | 密封面高度。ASME B16.5：Class 150/300 为 1.5，Class 400–2500 为 6.4；B16.47：Class 150/300 为 1.5，Class 400–900 为 6.4；EN/JIS：2.0（表另有规定除外） |
| `bc` | 小数（mm） | 否 | 螺栓孔中心圆直径 K |
| `bolts` | 整数 | 否 | 螺栓孔数量 |
| `bolt_hole` | 小数（mm） | 否 | 螺栓孔直径 |
| `hub` | 小数（mm） | 是 | 轮毂直径；盲板没有该尺寸（键可能缺失或为 `null`） |
| `length` | 小数（mm） | 是 | 轮毂长度 / 总长；盲板为 `null` |
| `hub_weld` | 小数（mm） | 是 | 焊接端直径；盲板为 `null` |
| `weight` | 小数（kg） | 是 | 标准表公布的近似重量；未公布为 `null` |

规则：

- **精度**：尺寸保留到 0.1 mm（标准表给到 0.01 mm 的除外）；重量 0.1 kg。
- **空值**：JSON 用 `null`（盲板的 `hub`/`length`/`hub_weld` 也可能整个键不存在）；CSV 用空字符串；SQLite 用 `NULL`。不要把空值当 0。
- **同一法兰同一螺栓模式**：同一 `std + size + rating` 下，`od`、`bc`、`bolts`、`bolt_hole` 在三种类型之间相同；只有 `th` 与类型相关的 `hub`/`length`/`hub_weld` 不同。`scripts/validate.py` 会检查这一点。
- **英制派生列**（仅 `dimensions.csv` / `flange_dimension` 中的 `*_in`）：`值 / 25.4`，四舍五入到 4 位小数，仅 ASME 三套有值，EN/JIS 为 `NULL`。
  ASME B16.47-2025 用精确米制换算（例如 36.50 in 印作 927 mm），反算出的英制值与标准英制表可能有第 3–4 位差异；需要标准英制值时以 `data/reference/asme-b16-47.json` 中的 U.S. Customary 表为准。

## 2. 标准元数据（`data/standards.json`）

| 字段 | 说明 |
|---|---|
| `standards[]` | 5 套尺寸数据各一条 |
| `id` / `slug` / `name` / `short` | 标准标识、URL slug、全名、短名 |
| `edition` | 参考数据版本（如 `ASME B16.5-2025`） |
| `dimensionEdition` | 尺寸数据版本（存在时优先于 `edition`） |
| `referenceDocumentId` | 对应的参考文档 id；ASME B16.47 Series A/B 共用 `asme-b16-47` |
| `editionNote` | 尺寸数据的版本核对说明 |
| `region` / `ratingName` / `sizeSyntax` | 适用地区、压力等级叫法、尺寸写法 |
| `typeNames` / `typeNotes` | 三种法兰类型的显示名与说明 |
| `baseMaterial` / `materialOptions` | 常用基础材料与可选材料 |
| `ratingNote` | 压力等级解释（适用于页面说明） |
| `intro[]` / `faceNote` | 标准简介与密封面说明（项目自行撰写） |
| `reference` | 对应的参考文档标识 |
| `referenceStandards[]` | 参考数据的标准标识（包含 B16.47 独立 id） |
| `topicLabels` | 参考主题的显示名 |

## 3. 参考数据（`data/reference/*.json`）

```jsonc
{
  "id": "asme-b16-5",
  "edition": "ASME B16.5-2025",
  "verifiedOn": "2026-11-04",
  "sourceDocument": "…",
  "topics": {
    "pressure-ratings": {
      "title": "…",
      "metaDescription": "…",
      "intro": ["…"],
      "keyFacts": [{ "label": "…", "value": "…" }],
      "sections": [
        {
          "heading": "…",
          "paragraphs": ["…"],
          "bullets": ["…"],
          "table": {
            "caption": "Table 2-1.1 (printed p. 25) — …",
            "note": "…",
            "columns": [{ "key": "temp", "label": "Temperature", "unit": "°C", "align": "left" }],
            "rows": [["38", "19.6", "51.0"]],
            "footnotes": [{ "marker": "a", "text": "…" }]
          }
        }
      ],
      "faq": [{ "q": "…", "a": "…" }],
      "sources": ["ASME B16.5-2025, Table 2-1.1 (printed p. 25)"],
      "related": [{ "label": "…", "href": "…" }]
    }
  }
}
```

| 层级 | 字段 | 说明 |
|---|---|---|
| 文档 | `id` | 参考标准标识，与 `data/reference/<id>.json` 文件名一致 |
| 文档 | `edition` / `verifiedOn` / `sourceDocument` | 版本、逐单元格核对日期、来源文档 |
| 文档 | `topics` | 主题字典；只包含有真实数据的主题 |
| 主题 | `title` / `metaDescription` / `intro` | 标题、摘要、导语（项目自行撰写） |
| 主题 | `keyFacts` | 可选的关键事实 |
| 主题 | `sections[]` | 章节；含 `heading`、`paragraphs`、`bullets`，可选 `table` |
| 主题 | `faq[]` | 常见问题 |
| 主题 | `sources[]` | 来源引用：标准、表号/条款、PDF 页码 |
| 表 | `caption` | 原表编号 + 印刷页码 |
| 表 | `note` | 读表规则、单位、脚注/斜体说明 |
| 表 | `columns[]` | `key`、`label`、`unit`、`align` |
| 表 | `rows[][]` | **位置对应列**，每行单元格数必须等于列数；**全部为字符串** |
| 表 | `footnotes[]` | `marker` + 解释 |

单元格数据规则：

- 全部按字符串保存：`"2,5"`（印刷的小数逗号）、`"−0.5"`、`"—"`（无值/不适用）、`"1,8 a)"`（含脚注标记）保持原样。
- 不要把单元格转换成数字后再落库，否则会丢失尾零、符号和脚注。
- 数值解释以每张表的 `note` 和原标准为准。

## 4. SQLite 结构（`sqlite/flange-standards.sqlite`）

| 表 | 行数 | 主键 / 说明 |
|---|---|---|
| `standard` | 5 | `id`；标准元数据 |
| `standard_type` | 15 | `(std, type)`；类型显示名 |
| `standard_rating` | 27 | `(std, rating)`；压力等级显示名 + 标准顺序 |
| `standard_size` | 101 | `(std, size)`；尺寸显示名 + DN |
| `flange_dimension` | 1,348 | `id`；尺寸主表，`UNIQUE(std, type, size, rating)` |
| `reference_topic` | 20 | `id`（`<std>::<topic>`）；主题元数据 |
| `reference_section` | 84 | 章节 |
| `reference_table` | 60 | `id`（`<std>::<topic>::t<n>`）；表说明 |
| `reference_column` | 365 | `(table_id, ord)`；列定义 |
| `reference_cell` | 7,345 | `(table_id, row_ord, col_ord)`；单元格文本 |
| `reference_footnote` | 106 | 表脚注 |
| `reference_faq` | 82 | 主题 FAQ |
| `reference_source` | 108 | 主题来源引用 |
| `v_reference_cell` | 视图 | 主题 + 表 + 列 + 单元格拼平，便于临时查询 |

索引：`flange_dimension(std, type, rating, dn)`、`flange_dimension(std, size)`。

> `standard_type` 是**标准类型词表**（上游元数据对所有标准统一列出 3 种类型），不代表该标准的数据里一定存在这种类型。例如 ASME B16.47 词表里有 `slip-on`，但 B16.47 标准本身不定义滑套法兰。需要按数据过滤时用
> `SELECT DISTINCT type FROM flange_dimension WHERE std = ?`。

## 5. 枚举值

| 枚举 | 值 |
|---|---|
| 标准（尺寸） | `asme-b16-5`、`asme-b16-47-a`、`asme-b16-47-b`、`jis-b2220`、`en-1092-1` |
| 标准（参考） | `asme-b16-5`、`asme-b16-47`、`en-1092-1`、`jis-b2220` |
| 法兰类型 | `weld-neck`、`slip-on`、`blind` |
| ASME 压力等级 | `150`、`300`、`400`、`600`、`900`、`1500`、`2500` |
| ASME B16.47 压力等级 | `150`、`300`、`400`、`600`、`900` |
| JIS 压力等级 | `5k`、`10k`、`16k`、`20k`、`30k` |
| EN 压力等级 | `pn6`、`pn10`、`pn16`、`pn25`、`pn40` |
| 参考主题 | `pressure-ratings`、`material-groups`、`tolerances`、`marking`、`changes`、`types`、`facings` |
