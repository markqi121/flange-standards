"""English UI labels and copy for the /en/ site."""

TYPES = {
    'weld-neck': {
        'note_en': 'Butt-welded to the pipe with a tapered hub; used for high pressure, high temperature and cyclic service.',
    },
    'slip-on': {
        'note_en': 'The pipe slips through the bore and is fillet-welded on both sides; economical for low-pressure utility lines.',
    },
    'blind': {
        'note_en': 'A solid disc bolted over a line end or vessel opening for isolation, testing and future tie-ins.',
    },
}

FIELDS = [
    ('od', 'D', 'Outside diameter', 'mm'),
    ('th', 'C', 'Flange thickness / blind thickness', 'mm'),
    ('rf', 'D2', 'Raised face diameter', 'mm'),
    ('rf_height', 'f', 'Raised face height', 'mm'),
    ('bc', 'K', 'Bolt circle diameter', 'mm'),
    ('bolts', 'n', 'Number of bolt holes', 'pcs'),
    ('bolt_hole', 'd', 'Bolt hole diameter', 'mm'),
    ('hub', 'A', 'Hub diameter', 'mm'),
    ('length', 'H', 'Hub length / overall length', 'mm'),
    ('hub_weld', 'B', 'Welding-end diameter', 'mm'),
    ('weight', '', 'Approximate weight', 'kg'),
]

PRINT_NOTES = {
    'asme-b16-5': 'Tolerances, facing finish, material and marking requirements: ASME B16.5-2025 Sections 6 and 7.',
    'asme-b16-47-a': 'Tolerances, material and marking: ASME B16.47-2025. Series A and Series B are not interchangeable.',
    'asme-b16-47-b': 'Tolerances, material and marking: ASME B16.47-2025. Series B and Series A are not interchangeable.',
    'en-1092-1': 'Tolerances, material groups and marking: EN 1092-1:2018, including Annex G.',
    'jis-b2220': 'Tolerances, material groups and marking: JIS B2220:2012, including Table 11.',
}

DISCLAIMER = (
    'This sheet is a factual compilation of published standards tables and is not a substitute '
    'for the standard itself. Verify dimensions and ratings against the edition named in your '
    'purchase specification before quoting, manufacturing or inspecting a critical part.'
)

SEARCH_EXAMPLES = [
    'DN100 PN16',
    '6" Class 150',
    'B16.5 4 inch 300 lb',
    '100A 10K slip-on',
]

TOPIC_LABELS = {
    'pressure-ratings': 'Pressure-temperature ratings',
    'material-groups': 'Material groups',
    'tolerances': 'Tolerances',
    'marking': 'Marking',
    'changes': 'Edition changes',
    'types': 'Types and facings',
}
