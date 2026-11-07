"""把 cataloa/reference_zh.py 的中文 overlay 合并到英文参考数据上。

只替换文字字段（标题、intro、章节说明、表头、脚注、FAQ），
表格 cells / 数值 / 表号 / 页码保留数据包原文。
"""

import copy


def _apply_table(table, overlay):
    if not isinstance(table, dict) or not isinstance(overlay, dict):
        return table
    for key in ('caption', 'note'):
        if overlay.get(key):
            table[key] = overlay[key]
    columns = table.get('columns') or []
    labels = overlay.get('columns') or []
    for index, label in enumerate(labels):
        if index < len(columns) and label:
            columns[index]['label'] = label
    footnotes = table.get('footnotes') or []
    texts = overlay.get('footnotes') or []
    for index, text in enumerate(texts):
        if index < len(footnotes) and text:
            footnotes[index]['text'] = text
    return table


def merge_topic(topic, overlay):
    """返回 merge 后的 copy；overlay 为空时原样返回 copy。"""
    merged = copy.deepcopy(topic)
    merged['title_en'] = topic.get('title', '')
    if not overlay:
        merged['has_zh'] = False
        return merged

    merged['has_zh'] = True
    for key in ('title', 'metaDescription', 'intro', 'keyFacts', 'faq', 'sources'):
        if overlay.get(key):
            merged[key] = copy.deepcopy(overlay[key])

    original_sections = merged.get('sections') or []
    translated_sections = overlay.get('sections') or []
    for index, section_overlay in enumerate(translated_sections):
        if index >= len(original_sections):
            break
        section = original_sections[index]
        for key in ('heading', 'paragraphs', 'bullets'):
            if section_overlay.get(key):
                section[key] = copy.deepcopy(section_overlay[key])
        if section_overlay.get('table'):
            section['table'] = _apply_table(section.get('table') or {}, section_overlay['table'])
    return merged
