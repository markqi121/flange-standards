-- 法兰标准数据包 · 示例查询
-- 用法：sqlite3 sqlite/flange-standards.sqlite < examples/queries.sql
-- 或进入交互后：.read examples/queries.sql

.headers on
.mode column

-- 0. 数据包总体情况
SELECT 'standards' AS item, COUNT(*) AS n FROM standard
UNION ALL SELECT 'dimension rows', COUNT(*) FROM flange_dimension
UNION ALL SELECT 'reference topics', COUNT(*) FROM reference_topic
UNION ALL SELECT 'reference tables', COUNT(*) FROM reference_table;

-- 1. 某规格的完整尺寸：ASME B16.5，NPS 6，Class 150
SELECT type, od_mm, th_mm, rf_mm, rf_height_mm, bc_mm,
       bolts, bolt_hole_mm, hub_mm, length_mm, weight_kg
FROM flange_dimension
WHERE std = 'asme-b16-5' AND size = '6' AND rating = '150'
ORDER BY type;

-- 2. 一个标准的尺寸范围与等级
SELECT rating, label FROM standard_rating WHERE std = 'jis-b2220' ORDER BY ord;
SELECT size, size_label, dn FROM standard_size WHERE std = 'jis-b2220' ORDER BY dn;

-- 3. 跨标准对比同一 DN（DN100 附近，对焊法兰）
SELECT std, size_label, rating_label, od_mm, bc_mm, bolts, bolt_hole_mm
FROM flange_dimension
WHERE type = 'weld-neck' AND dn = 100
  AND ((std = 'asme-b16-5' AND rating = '150')
    OR (std = 'en-1092-1' AND rating = 'pn16')
    OR (std = 'jis-b2220' AND rating = '10k'));

-- 4. 定位压力—温度表，并查看列定义
SELECT id, ord, caption FROM reference_table
WHERE topic_id = 'asme-b16-5::pressure-ratings' ORDER BY ord;

SELECT ord, key, label, unit FROM reference_column
WHERE table_id = 'asme-b16-5::pressure-ratings::t3' ORDER BY ord;

-- 5. 透视 ASME B16.5 Group 1.1（SI）的温度—压力
SELECT
  MAX(CASE WHEN vc.col_ord = 0 THEN vc.value END) AS temperature_c,
  MAX(CASE WHEN vc.col_ord = 1 THEN vc.value END) AS class_150_bar,
  MAX(CASE WHEN vc.col_ord = 2 THEN vc.value END) AS class_300_bar,
  MAX(CASE WHEN vc.col_ord = 3 THEN vc.value END) AS class_400_bar
FROM v_reference_cell vc
WHERE vc.table_id = 'asme-b16-5::pressure-ratings::t3'
GROUP BY vc.row_ord
ORDER BY vc.row_ord;

-- 6. 材料组：ASME B16.5 Group 2
SELECT DISTINCT
  MAX(CASE WHEN vc.col_ord = 0 THEN vc.value END) AS material_group,
  MAX(CASE WHEN vc.col_ord = 1 THEN vc.value END) AS nominal_designation,
  MAX(CASE WHEN vc.col_ord = 2 THEN vc.value END) AS pt_table
FROM v_reference_cell vc
WHERE vc.table_id = 'asme-b16-5::material-groups::t2'
GROUP BY vc.row_ord
ORDER BY vc.row_ord;

-- 7. 来源与核对日期
SELECT t.title, t.edition, t.verified_on, s.citation
FROM reference_topic t
LEFT JOIN reference_source s ON s.topic_id = t.id
WHERE t.id = 'jis-b2220::pressure-ratings'
ORDER BY s.ord;
