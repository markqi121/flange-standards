"""查询语句解析：把中文/英文混合的搜索词翻译成数据库过滤条件。

支持的写法示例：
- `DN100 PN16`
- `6″ Class 150`
- `B16.5 4 寸 300 磅`
- `100A 10K 滑套`
- `JIS B2220 8K 盲板`
- `ASME B16.47 36 英寸 900 磅`
"""

import re
import unicodedata
from dataclasses import dataclass, field

from django.db.models import Q

from .models import DimensionRow

# NPS（英寸）→ DN（mm）
INCH_TO_DN = {
    0.5: 15, 0.75: 20, 1: 25, 1.25: 32, 1.5: 40, 2: 50, 2.5: 65, 3: 80,
    3.5: 90, 4: 100, 5: 125, 6: 150, 8: 200, 10: 250, 12: 300, 14: 350,
    16: 400, 18: 450, 20: 500, 22: 550, 24: 600, 26: 650, 28: 700, 30: 750,
    32: 800, 34: 850, 36: 900, 38: 950, 40: 1000, 42: 1050, 44: 1100,
    46: 1150, 48: 1200, 50: 1250, 52: 1300, 54: 1350, 56: 1400, 58: 1450, 60: 1500,
}

CLASS_CODES = {'150', '300', '400', '600', '900', '1500', '2500'}
K_CODES = {'5', '10', '16', '20', '30'}

TYPE_WORDS = [
    ('weld-neck', ['对焊', '焊颈', 'weld neck', 'weld-neck', 'wn']),
    ('slip-on', ['滑套', '平焊', '滑套式', 'slip on', 'slip-on', 'so']),
    ('blind', ['盲板', '盲法兰', '盲', 'blind', 'blank']),
]

STANDARD_WORDS = [
    ('en-1092-1', ['en1092', 'en 1092', 'en-1092', 'din', '欧标']),
    ('jis-b2220', ['jisb2220', 'jis b2220', 'jis', '日标']),
]


@dataclass
class ParsedQuery:
    standard_ids: list = field(default_factory=list)
    type_slugs: list = field(default_factory=list)
    rating_code: str = ''
    dn: int = 0
    keywords: list = field(default_factory=list)
    chips: list = field(default_factory=list)
    unrecognized: list = field(default_factory=list)


def _normalize(text):
    text = unicodedata.normalize('NFKC', text or '')
    return (
        text.lower()
        .replace('”', '"')
        .replace('“', '"')
        .replace('″', '"')
        .replace('′', '"')
        .replace('’', "'")
        .replace('，', ',')
    )


def _parse_standard(text, parsed):
    if re.search(r'b\s*16\.?\s*47', text):
        if re.search(r'(series\s*a|series-a|\ba系列|\bseries\s*a\b)', text) or re.search(
            r'16\.?\s*47\s*a\b', text
        ):
            parsed.standard_ids = ['asme-b16-47-a']
        elif re.search(r'(series\s*b|series-b|\bb系列)', text) or re.search(
            r'16\.?\s*47\s*b\b', text
        ):
            parsed.standard_ids = ['asme-b16-47-b']
        else:
            parsed.standard_ids = ['asme-b16-47-a', 'asme-b16-47-b']
        parsed.chips.append('ASME B16.47')
        return
    if re.search(r'b\s*16\.?\s*5\b', text):
        parsed.standard_ids = ['asme-b16-5']
        parsed.chips.append('ASME B16.5')
        return
    for standard_id, words in STANDARD_WORDS:
        if any(word in text for word in words):
            parsed.standard_ids = [standard_id]
            parsed.chips.append('EN 1092-1' if standard_id.startswith('en') else 'JIS B2220')
            return


def _parse_type(text, parsed):
    for slug, words in TYPE_WORDS:
        for word in words:
            if re.search(rf'(?<![a-z]){re.escape(word)}(?![a-z])', text):
                parsed.type_slugs = [slug]
                parsed.chips.append({'weld-neck': '对焊', 'slip-on': '滑套', 'blind': '盲板'}[slug])
                return


def _parse_rating(text, parsed):
    patterns = [
        (r'class\s*(\d{2,4})', lambda m: m.group(1)),
        (r'(\d{2,4})\s*(?:磅|#|lb\b|lbs\b|pound)', lambda m: m.group(1)),
        (r'pn\s*(\d+(?:[.,]\d+)?)', lambda m: 'pn' + m.group(1).replace(',', '.')),
        (r'(\d{1,2})\s*k\b', lambda m: m.group(1) + 'k'),
    ]
    for pattern, mapper in patterns:
        match = re.search(pattern, text)
        if match:
            parsed.rating_code = mapper(match)
            code = parsed.rating_code
            if code in CLASS_CODES:
                chip = f'Class {code}'
            elif code.startswith('pn'):
                chip = 'PN' + code[2:].upper()
            elif code.endswith('k'):
                chip = code[:-1].upper() + 'K'
            else:
                chip = code.upper()
            parsed.chips.append(chip)
            return

    # 16 公斤 / 16 bar：只对 EN 的 PN 体系有意义
    match = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:公斤|bar|巴)\b', text)
    if match and parsed.standard_ids and parsed.standard_ids[0].startswith('en'):
        parsed.rating_code = 'pn' + match.group(1).replace(',', '.')
        parsed.chips.append('PN' + match.group(1))


def _parse_size(text, parsed):
    match = re.search(r'dn\s*(\d{1,4})', text)
    if match:
        parsed.dn = int(match.group(1))
        parsed.chips.append(f'DN{parsed.dn}')
        return

    match = re.search(r'(\d{1,3})\s*a\b', text)
    if match:
        parsed.dn = int(match.group(1))
        parsed.chips.append(f'{parsed.dn}A')
        return

    match = re.search(
        r'(\d{1,2}(?:[- ]\d{1,2}/\d{1,2})?|\d{1,2}/\d{1,2})\s*(?:寸|英寸|"|\bin\b|\binch\b)',
        text,
    )
    if match:
        inches = _inches(match.group(1))
        if inches and _nearest_dn(inches):
            parsed.dn = _nearest_dn(inches)
            parsed.chips.append(f'{match.group(1)}″ / DN{parsed.dn}')
        return

    match = re.search(r'nps\s*(\d{1,2}(?:[- ]\d{1,2}/\d{1,2})?)', text)
    if match:
        inches = _inches(match.group(1))
        if inches and _nearest_dn(inches):
            parsed.dn = _nearest_dn(inches)
            parsed.chips.append(f'NPS {match.group(1)} / DN{parsed.dn}')
        return


def _inches(text):
    text = text.strip()
    try:
        if '/' in text and '-' in text:
            whole, fraction = text.split('-', 1)
            return float(whole) + _fraction(fraction)
        if '/' in text:
            return _fraction(text)
        return float(text)
    except (TypeError, ValueError):
        return None


def _fraction(text):
    if '/' not in text:
        return float(text)
    numerator, denominator = text.split('/', 1)
    return float(numerator) / float(denominator)


def _nearest_dn(inches):
    if not inches:
        return 0
    best = min(INCH_TO_DN, key=lambda value: abs(value - inches))
    return INCH_TO_DN[best] if abs(best - inches) < 0.05 else 0


def _parse_bare_numbers(text, parsed):
    """没有单位的数字：≤24 当英寸尺寸，其余当 DN；Class 必须写“磅/Class”。"""
    for token in re.findall(r'(?<![\w./-])(\d{1,4})(?![\w/.-])', text):
        if parsed.dn:
            continue
        number = int(token)
        if number <= 24:
            dn = INCH_TO_DN.get(float(number)) or _nearest_dn(float(number))
            if dn:
                parsed.dn = dn
                parsed.chips.append(f'{token}″ / DN{dn}')
        elif 25 <= number <= 1500:
            parsed.dn = number
            parsed.chips.append(f'DN{number}')


# 已经被其他规则消费掉的写法，不再当自由关键词去匹配，避免 “4 寸 300 磅” 里
# 的 “寸”“磅” 把结果全部过滤掉。
_CONSUMED_PATTERNS = [
    r'b\s*16\.?\s*47\s*[ab]?',
    r'b\s*16\.?\s*5',
    r'en\s*-?\s*1092(?:-1)?',
    r'jis\s*b?2220',
    r'class\s*\d+',
    r'\d+\s*(?:磅|#|lbs?|pounds?)',
    r'pn\s*\d+(?:[.,]\d+)?',
    r'\d+\s*k\b',
    r'\d+(?:[.,]\d+)?\s*(?:公斤|bars?|巴)',
    r'dn\s*\d+',
    r'\d+\s*a\b',
    r'nps\s*\d+(?:[- ]\d+/\d+)?',
    r'\d+(?:[- ]\d+/\d+)?\s*(?:寸|英寸|"|′|\bin\b|\binch\b)',
    r'(?<![a-z])(?:asme|en|jis|din|ansi)(?![a-z])',
    r'(?<![a-z])(?:weld\s*neck|weld-neck|slip\s*on|slip-on|blind)(?![a-z])',
    r'对焊|焊颈|滑套|平焊|盲板|盲法兰|盲|法兰|标准|规格|尺寸',
]

_KEYWORD_STOPWORDS = {
    'flange', 'flanges', 'standard', 'standards', 'spec', 'specification',
    'the', 'and', 'for', 'with', 'size', 'class',
}


def _parse_keywords(text, parsed):
    remainder = text
    for pattern in _CONSUMED_PATTERNS:
        remainder = re.sub(pattern, ' ', remainder)
    for word in re.split(r'[\s,;，；/]+', remainder):
        word = word.strip('()[]{}【】')
        if not word:
            continue
        if re.fullmatch(r'[\d.,″′"#\-]+', word):
            continue
        if word.lower() in _KEYWORD_STOPWORDS:
            continue
        parsed.keywords.append(word)


def parse(text):
    parsed = ParsedQuery()
    normalized = _normalize(text)
    _parse_standard(normalized, parsed)
    _parse_type(normalized, parsed)
    _parse_rating(normalized, parsed)
    _parse_size(normalized, parsed)
    _parse_bare_numbers(normalized, parsed)
    _parse_keywords(normalized, parsed)
    return parsed


def queryset_for(parsed):
    queryset = DimensionRow.objects.select_related(
        'standard', 'flange_type', 'rating'
    ).all()

    if parsed.standard_ids:
        queryset = queryset.filter(standard_id__in=parsed.standard_ids)
    if parsed.type_slugs:
        queryset = queryset.filter(flange_type_id__in=parsed.type_slugs)
    if parsed.rating_code:
        queryset = queryset.filter(rating__code=parsed.rating_code)
    if parsed.dn:
        queryset = queryset.filter(dn=parsed.dn)

    for keyword in parsed.keywords:
        queryset = queryset.filter(
            Q(standard__name__icontains=keyword)
            | Q(standard__name_zh__icontains=keyword)
            | Q(standard__short__icontains=keyword)
            | Q(flange_type__name_zh__icontains=keyword)
            | Q(flange_type__name_en__icontains=keyword)
            | Q(size_label__icontains=keyword)
            | Q(rating__label__icontains=keyword)
        )

    return queryset


def search(text):
    parsed = parse(text)
    return queryset_for(parsed), parsed
