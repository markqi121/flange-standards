# 查询指南（SQL / JSON）

示例均以 `sqlite/flange-standards.sqlite` 为准，可直接粘贴运行：

```bash
sqlite3 sqlite/flange-standards.sqlite
.headers on
.mode column
```

## 1. 按「标准 + 类型 + 尺寸 + 压力等级」取一行

```sql
SELECT od_mm, th_mm, rf_mm, rf_height_mm, bc_mm,
       bolts, bolt_hole_mm, hub_mm, length_mm, hub_weld_mm, weight_kg
FROM flange_dimension
WHERE std = 'asme-b16-5'
  AND type = 'weld-neck'
  AND size = '6'
  AND rating = '150';
```

`std`/`type`/`rating` 是枚举值；`size` 是稳定的 slug（见 `docs/fields.md` 第 5 节）。
该查询命中 `std, type, size, rating` 上的唯一索引（`idx_dimension_lookup` 也可作为前缀使用）。

## 2. 看一个标准有哪些类型、等级、尺寸

```sql
-- 类型
SELECT * FROM standard_type WHERE std = 'asme-b16-5';

-- 压力等级（按标准顺序）
SELECT rating, label FROM standard_rating WHERE std = 'asme-b16-5' ORDER BY ord;

-- 尺寸（按 DN 排序）
SELECT size, size_label, dn FROM standard_size WHERE std = 'asme-b16-5' ORDER BY dn;

-- 某等级下每个尺寸的三种类型行数
SELECT size_label, dn, type, COUNT(*) AS rows
FROM flange_dimension
WHERE std = 'asme-b16-5' AND rating = '150'
GROUP BY size_label, dn, type
ORDER BY dn, type;
```

## 3. 跨标准对比同一 DN

`dn` 把 ASME 的 NPS、JIS 的 A 数、EN 的 DN 归一到一个字段，适合做互换性对比：

```sql
SELECT std, size_label, rating_label, od_mm, bc_mm, bolts, bolt_hole_mm
FROM flange_dimension
WHERE type = 'weld-neck' AND dn = 100
  AND ((std = 'asme-b16-5' AND rating = '150')
    OR (std = 'en-1092-1' AND rating = 'pn16')
    OR (std = 'jis-b2220' AND rating = '10k'));
```

结果（DN100 / 约 Class 150 档）：

| std | size_label | rating_label | od_mm | bc_mm | bolts | bolt_hole_mm |
|---|---|---|---|---|---|---|
| asme-b16-5 | 4″ | Class 150 | 228.6 | 190.5 | 8 | 19.1 |
| en-1092-1 | DN100 | PN16 | 220.0 | 180.0 | 8 | 18.0 |
| jis-b2220 | 100A | 10K | 210.0 | 175.0 | 8 | 19.0 |

> 三套标准的 OD 和螺栓圆都不同，不能混装。**同 DN 不等于可互换。**

## 4. 压力—温度（p/T）额定值

p/T 表是按标准排版的宽表，列数和列含义各不相同。推荐两步走：

**第一步：定位表**。表 id 规则是 `<参考标准>::<主题>::t<序号>`：

```sql
SELECT id, ord, caption
FROM reference_table
WHERE topic_id = 'asme-b16-5::pressure-ratings'
ORDER BY ord;
```

**第二步：看列定义**（`ord` 就是 `reference_cell.col_ord`）：

```sql
SELECT ord, key, label, unit
FROM reference_column
WHERE table_id = 'asme-b16-5::pressure-ratings::t3'
ORDER BY ord;
```

Result: `0 = Temperature °C`，`1 = Class 150 bar`，`2 = Class 300 bar` … `7 = Class 2500 bar`。

**第三步：透视成「温度 + 等级」两列**：

```sql
SELECT
  MAX(CASE WHEN vc.col_ord = 0 THEN vc.value END) AS temperature_c,
  MAX(CASE WHEN vc.col_ord = 1 THEN vc.value END) AS class_150_bar,
  MAX(CASE WHEN vc.col_ord = 2 THEN vc.value END) AS class_300_bar
FROM v_reference_cell vc
WHERE vc.table_id = 'asme-b16-5::pressure-ratings::t3'
GROUP BY vc.row_ord
ORDER BY vc.row_ord;
```

结果摘录：

| temperature_c | class_150_bar | class_300_bar |
|---|---|---|
| Min. to 38 [Note (6)] | 19.6 | 51.0 |
| 50 | 19.1 | 49.8 |
| 100 | 17.7 | 46.7 |
| 200 | 13.8 | 43.8 |

注意：

- 单元格是**字符串**，包含 `Min. to 38 [Note (6)]`、小数逗号、`—` 等原始写法；需要数值时在应用层清洗（例如 `CAST(REPLACE(value,' ','') AS REAL)`，并把 `—` 视为空）。
- 温度行、材料组列的具体含义以 `reference_table.caption` / `note` 和原标准为准。
- 不要跨表拼接：每张 p/T 表的列对应不同材料组。

## 5. 材料组

```sql
-- ASME B16.5 的 Group 2 材料表（Table 1.1-1）
SELECT id, caption FROM reference_table
WHERE topic_id = 'asme-b16-5::material-groups' ORDER BY ord;
```

```sql
-- 透视 Table 1.1-1（group 2）：材料组 + 名义牌号 + 对应 p/T 表
SELECT DISTINCT
  MAX(CASE WHEN vc.col_ord = 0 THEN vc.value END) AS material_group,
  MAX(CASE WHEN vc.col_ord = 1 THEN vc.value END) AS nominal_designation,
  MAX(CASE WHEN vc.col_ord = 2 THEN vc.value END) AS pt_table
FROM v_reference_cell vc
WHERE vc.table_id = 'asme-b16-5::material-groups::t2'
GROUP BY vc.row_ord
ORDER BY vc.row_ord;
```

EN 1092-1 的材料组（`en-1092-1::material-groups::t1`）列序为：材料组、说明、典型 EN 牌号。
JIS B2220 的材料组见 `jis-b2220::material-groups::t1`。

## 6. 公差、标记、版本变更

三个主题结构一致，按 topic 直接取表：

```sql
-- 公差表清单
SELECT id, caption FROM reference_table
WHERE topic_id = 'en-1092-1::tolerances' ORDER BY ord;

-- 标记要求（第 0 列通常是标记项目，第 1 列是要求）
SELECT
  MAX(CASE WHEN col_ord = 0 THEN value END) AS item,
  MAX(CASE WHEN col_ord = 1 THEN value END) AS requirement
FROM v_reference_cell
WHERE table_id = 'asme-b16-5::marking::t1'
GROUP BY row_ord ORDER BY row_ord;

-- 版本变更（2025 版）
SELECT caption FROM reference_table WHERE topic_id = 'asme-b16-5::changes';
```

## 7. 来源与核对日期

每个主题都带版本、核对日期和来源引用，查询功能应把它们一起展示：

```sql
SELECT t.title, t.edition, t.verified_on, s.citation
FROM reference_topic t
LEFT JOIN reference_source s ON s.topic_id = t.id
WHERE t.id = 'jis-b2220::pressure-ratings'
ORDER BY s.ord;
```

`data/manifest.json` 里还有整包版本（`version`）和每个文件的 SHA-256。

## 8. 导出

```bash
# 尺寸全表导出
sqlite3 -header -csv sqlite/flange-standards.sqlite \
  "SELECT * FROM flange_dimension;" > dimensions-export.csv

# 某标准某等级导出
sqlite3 -header -csv sqlite/flange-standards.sqlite \
  "SELECT * FROM flange_dimension WHERE std='jis-b2220' AND rating='10k';" \
  > jis-10k.csv
```

## 9. 在代码里使用

**Python（标准库）**

```python
import sqlite3

conn = sqlite3.connect("sqlite/flange-standards.sqlite")
conn.row_factory = sqlite3.Row
row = conn.execute(
    """SELECT * FROM flange_dimension
       WHERE std=? AND type=? AND size=? AND rating=?""",
    ("asme-b16-5", "weld-neck", "6", "150"),
).fetchone()
print(dict(row))
```

**Node.js**

```js
import { DatabaseSync } from 'node:sqlite'; // Node 22.5+

const db = new DatabaseSync('sqlite/flange-standards.sqlite');
const rows = db.prepare(
  'SELECT * FROM flange_dimension WHERE std = ? AND dn = ?',
).all('en-1092-1', 100);
console.log(rows);
```

**直接读 JSON（不依赖数据库）**

```js
import dims from '../data/dimensions/jis-b2220.json' with { type: 'json' };
const rows = dims.filter((r) => r.type === 'slip-on' && r.rating === '10k');
```

## 10. 性能

- `flange_dimension` 只有 1,348 行，任何查询都是毫秒级，不需要额外缓存。
- 常用条件用 `std + type + rating + dn`（有索引）；只按 `std` 或 `size` 也有索引。
- `reference_cell` 有 7,345 行，按 `table_id` 查询即可；透视/聚合在应用层或 SQL 里都很轻。
- 如果后续做成 Web 服务，建议在启动时只读打开数据库（`?mode=ro`），并保留 `v_reference_cell` 视图。

## 11. 给后续查询功能的接口草案

数据包本身不含服务端代码；如果后续做成独立查询功能，推荐按下面的形状暴露接口，直接查询 SQLite：

```http
GET /api/flange/dimensions?std=asme-b16-5&type=weld-neck&size=6&rating=150
```

```jsonc
{
  "items": [
    {
      "type": "weld-neck",
      "size_label": "6″",
      "rating_label": "Class 150",
      "od_mm": 279.4, "th_mm": 23.9, "rf_mm": 215.9, "rf_height_mm": 1.5,
      "bc_mm": 241.3, "bolts": 8, "bolt_hole_mm": 22.2,
      "hub_mm": 192.0, "length_mm": 87.4, "hub_weld_mm": 168.4, "weight_kg": 10.9
    }
  ],
  "meta": {
    "std": "asme-b16-5",
    "edition": "ASME B16.5-2025",
    "verifiedOn": "2026-11-04",
    "source": "ASME B16.5-2025, dimension tables"
  }
}
```

```http
GET /api/flange/pressure-ratings?std=asme-b16-5&group=1.1&temperatureC=200
```

- p/T 查询要先定位材料组对应的表（见第 4 节），再读对应温度行；返回体里带上 `caption`、`verifiedOn` 和 `source`。
- 材料组、公差、标记、版本变更可以统一走 `GET /api/flange/reference?std=&topic=`，返回主题元数据 + 结构化表格。
- 空值统一用 `null`；参考表的单元格原样返回字符串（小数逗号、`—`、脚注标记不转换），由前端按 `note` 解释。
- 所有响应都建议带 `edition` 和 `verifiedOn`；页面上始终展示「以购买的标准版本为准」。
