"""中文文案：标准说明、类型说明、字段名、参考主题名。

数据包本身是英文的；这里集中维护中文展示层，避免中英文散落在模板里。
后台（Django admin）可以继续修改 Standard 上的中文简介与说明字段。
"""

STANDARDS = {
    'asme-b16-5': {
        'name_zh': 'ASME B16.5 管法兰与法兰管件',
        'region': '北美、中东、东南亚',
        'intro_zh': [
            'ASME B16.5 是美标管法兰的基础标准，覆盖 NPS 1/2 至 NPS 24、Class 150 至 Class 2500。'
            '对焊、滑套、盲板法兰的外径、厚度、密封面、螺栓孔中心圆、螺栓孔数量与直径都按此标准。',
            '同一尺寸、同一压力等级、同一密封面的 B16.5 法兰可以互换；不同压力等级、不同密封面，'
            '或与 B16.47 大直径法兰之间不能互换。Class 不是固定压力，实际允许压力要按材料组和设计温度查压力-温度表。',
        ],
        'rating_name_zh': 'Class 压力等级',
        'rating_note_zh': 'ASME 的 Class 是压力等级代号，不是固定工作压力。以 A105（材料组 1.1）为例：'
        'Class 150 在 38 °C 的允许压力为 19.6 bar，Class 300 为 51.0 bar，Class 600 为 102.0 bar，'
        '温度升高后按压力-温度表折减。选型时必须按设计温度与材料组查表。',
        'edition_note_zh': '尺寸按 ASME B16.5-2025 的 SI 表逐格转录，并与英制表核对；'
        '密封面高度按 2025 版规则：Class 150/300 为 1.5 mm，Class 400–2500 为 6.4 mm。',
    },
    'asme-b16-47-a': {
        'name_zh': 'ASME B16.47 Series A 大直径钢制法兰',
        'region': '北美、中东、东南亚',
        'intro_zh': [
            'ASME B16.47 覆盖 NPS 26 至 NPS 60 的大直径钢制法兰，分为 Series A 和 Series B 两套尺寸体系。'
            'Series A 沿用原 MSS SP-44 的尺寸，法兰外径和螺栓数量通常比 Series B 大。',
            'B16.47 只定义对焊法兰和盲板法兰，没有滑套法兰。Series A 与 Series B 的螺栓孔中心圆和螺栓数量不同，'
            '两套法兰不能互换；与 B16.5 同尺寸法兰也不能互换。',
        ],
        'rating_name_zh': 'Class 压力等级',
        'rating_note_zh': '压力等级与 ASME B16.5 一样采用 Class 代号，允许压力按材料组和温度查 B16.47-2025 的压力-温度表。'
        'Series A 与 Series B 的压力等级范围不同，选型时注意标准版本与系列。',
        'edition_note_zh': '尺寸按 ASME B16.47-2025 Series A 的 SI 表转录，并与英制表核对。',
    },
    'asme-b16-47-b': {
        'name_zh': 'ASME B16.47 Series B 大直径钢制法兰',
        'region': '北美、中东、东南亚',
        'intro_zh': [
            'ASME B16.47 Series B 沿用原 API 605 的紧凑型尺寸，同尺寸下外径和螺栓数量比 Series A 小，'
            '常用于空间受限的大直径管道。',
            'B16.47 只定义对焊法兰和盲板法兰，没有滑套法兰。Series B 与 Series A、B16.5 均不能互换。',
        ],
        'rating_name_zh': 'Class 压力等级',
        'rating_note_zh': '压力等级采用 Class 代号，允许压力按材料组和温度查 B16.47-2025 的压力-温度表。'
        'Series B 的等级上限与 Series A 不同，选型时以标准原文为准。',
        'edition_note_zh': '尺寸按 ASME B16.47-2025 Series B 的 SI 表转录，并与英制表核对。',
    },
    'en-1092-1': {
        'name_zh': 'EN 1092-1 法兰及其连接件（PN 系列）',
        'region': '欧洲、中东、非洲',
        'intro_zh': [
            'EN 1092-1 是欧洲管法兰标准，采用 PN 公称压力体系，常用 DN10 至 DN600、PN6 至 PN40。'
            '标准按法兰类型编号（如 Type 11 对焊、Type 01/02 滑套、Type 05 盲板），密封面以 RF 突面为主。',
            'PN 是公称压力代号，不是允许工作压力；实际允许压力按材料组和温度查 EN 1092-1 附录 G 的压力-温度表。'
            'EN 法兰与 ASME、JIS 同尺寸法兰的螺栓孔中心圆和螺栓数量不同，不能直接互换。',
        ],
        'rating_name_zh': 'PN 公称压力',
        'rating_note_zh': 'PN 是公称压力代号。以非奥氏体材料组 3E0（P245GH，vR ≤ 50 mm）为例，'
        'PN16 在室温的允许压力为 16 bar，100 °C 时为 14.8 bar，200 °C 时为 13.3 bar；'
        '其他材料组与厚度等级按 EN 1092-1 附录 G 查表。',
        'edition_note_zh': '尺寸按 EN 1092-1:2018 的 SI 表转录；部分类型/尺寸标准表未列轮毂与长度，页面按空值显示。',
    },
    'jis-b2220': {
        'name_zh': 'JIS B2220 钢制管法兰（K 级）',
        'region': '日本、东南亚',
        'intro_zh': [
            'JIS B2220 是日本钢制管法兰标准，采用 K 级压力体系（5K、10K、16K、20K、30K），'
            '公称尺寸用 A 数表示（10A–600A），密封面以 RF 突面为主。',
            'JIS 法兰的螺栓孔中心圆、螺栓数量和直径与 ASME、EN 同尺寸法兰不同，不能直接互换；'
            '与日标设备、阀门配套时必须整套按 JIS 订货。',
        ],
        'rating_name_zh': 'K 级压力等级',
        'rating_note_zh': 'K 级是压力等级代号，不是固定工作压力。以 5K、材料组 1.1 为例，'
        '允许压力为 0.7 MPa（约 7 bar，至 120 °C），温度升高后按 JIS B2220 表 11 折减。',
        'edition_note_zh': '尺寸按 JIS B2220:2012 的 SI 表转录；部分类型的轮毂与重量标准未列，页面按空值显示。',
    },
}

TYPES = {
    'weld-neck': {
        'name_zh': '对焊法兰',
        'name_en': 'Weld Neck',
        'note_zh': '带锥形轮毂，与管道对焊连接；强度高、疲劳性能好，用于高压、高温和循环工况。',
    },
    'slip-on': {
        'name_zh': '滑套法兰',
        'name_en': 'Slip-On',
        'note_zh': '管道穿过法兰内孔后双面角焊；成本低、对中容易，多用于低压公用工程管线。',
    },
    'blind': {
        'name_zh': '盲板法兰',
        'name_en': 'Blind',
        'note_zh': '实心圆盘，用螺栓压紧在管线端部或设备开口上；用于隔离、试压和预留接口。',
    },
}

TOPIC_LABELS = {
    'pressure-ratings': '压力-温度额定值',
    'material-groups': '材料组',
    'tolerances': '尺寸公差',
    'marking': '标记要求',
    'changes': '版本变更',
    'types': '类型与密封面',
}

# (字段名, 符号, 中文名, 单位, 打印页是否显示)
FIELDS = [
    ('od', 'D', '外径', 'mm', True),
    ('th', 'C', '法兰厚度 / 盲板厚度', 'mm', True),
    ('rf', 'D2', '密封面直径', 'mm', True),
    ('rf_height', 'f', '密封面高度', 'mm', True),
    ('bc', 'K', '螺栓孔中心圆直径', 'mm', True),
    ('bolts', 'n', '螺栓孔数量', '个', True),
    ('bolt_hole', 'd', '螺栓孔直径', 'mm', True),
    ('hub', 'A', '轮毂直径', 'mm', True),
    ('length', 'H', '轮毂长度 / 总长', 'mm', True),
    ('hub_weld', 'B', '焊接端直径', 'mm', True),
    ('weight', '', '近似重量', 'kg', True),
]

DISCLAIMER = (
    '本页数据为公开标准表格的事实性整理，仅供选型与生产参考。正式报价、投标、'
    '关键件加工和检验前，请以项目指定的标准版本原文复核；本库不替代标准文本。'
)

PRINT_NOTES = {
    'asme-b16-5': '尺寸公差、密封面粗糙度、材料与标记要求见 ASME B16.5-2025 第 6、7 章及相应表格。',
    'asme-b16-47-a': '尺寸公差、材料与标记要求见 ASME B16.47-2025；Series A 与 Series B 不可互换。',
    'asme-b16-47-b': '尺寸公差、材料与标记要求见 ASME B16.47-2025；Series B 与 Series A 不可互换。',
    'en-1092-1': '尺寸公差、材料组与标记要求见 EN 1092-1:2018 相应条款与附录 G。',
    'jis-b2220': '尺寸公差、材料组与标记要求见 JIS B2220:2012 相应条款与表 11。',
}

# 首页快捷入口：标准 + 类型 + 尺寸 + 等级
QUICK_LINKS = [
    ('ASME B16.5 · Class 150 · NPS 6″ 对焊', 'asme-b16-5', 'weld-neck', '6', '150'),
    ('ASME B16.5 · Class 300 · NPS 4″ 滑套', 'asme-b16-5', 'slip-on', '4', '300'),
    ('ASME B16.5 · Class 150 · NPS 8″ 盲板', 'asme-b16-5', 'blind', '8', '150'),
    ('EN 1092-1 · PN16 · DN100 对焊', 'en-1092-1', 'weld-neck', 'dn100', 'pn16'),
    ('JIS B2220 · 10K · 100A 滑套', 'jis-b2220', 'slip-on', '100a', '10k'),
    ('ASME B16.47 A · Class 900 · 36″ 对焊', 'asme-b16-47-a', 'weld-neck', '36', '900'),
]

SEARCH_EXAMPLES = [
    'DN100 PN16',
    '6″ Class 150',
    'B16.5 4 寸 300 磅',
    '100A 10K 滑套',
    'JIS 盲板 16K',
]
