"""把 flange-standards-data 数据包导入数据库。

用法：
    python manage.py import_standards            # 增量导入 / 覆盖同名记录
    python manage.py import_standards --reset    # 先清空尺寸与参考数据再导入
    python manage.py import_standards --data-dir /path/to/flange-standards-data
"""

import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog import zh_content
from catalog.models import (
    DataImport,
    DimensionRow,
    FlangeType,
    Rating,
    ReferenceDoc,
    Standard,
)

DIMENSION_FIELDS = [
    'od', 'th', 'rf', 'rf_height', 'bc', 'bolts', 'bolt_hole',
    'hub', 'length', 'hub_weld', 'weight',
]


def _rating_sort(code):
    digits = ''.join(ch for ch in code if ch.isdigit() or ch == '.')
    try:
        return float(digits)
    except ValueError:
        return 0.0


class Command(BaseCommand):
    help = '从 flange-standards-data 导入标准、尺寸数据和参考数据'

    def add_arguments(self, parser):
        parser.add_argument('--data-dir', default=None, help='数据包目录')
        parser.add_argument('--reset', action='store_true', help='导入前清空尺寸/参考数据')

    def handle(self, *args, **options):
        data_dir = Path(options['data_dir'] or settings.DATA_DIR)
        if not data_dir.exists():
            raise CommandError(f'数据包不存在：{data_dir}')

        standards_meta = json.loads((data_dir / 'data/standards.json').read_text('utf-8'))
        manifest = json.loads((data_dir / 'data/manifest.json').read_text('utf-8'))

        with transaction.atomic():
            if options['reset']:
                DimensionRow.objects.all().delete()
                Rating.objects.all().delete()
                ReferenceDoc.objects.all().delete()

            standards = {}
            for index, meta in enumerate(standards_meta['standards']):
                zh = zh_content.STANDARDS.get(meta['id'], {})
                standard, _ = Standard.objects.update_or_create(
                    id=meta['id'],
                    defaults={
                        'name': meta['name'],
                        'name_zh': zh.get('name_zh', meta['name']),
                        'short': meta['short'],
                        'edition': meta['edition'],
                        'dimension_edition': meta.get('dimensionEdition') or '',
                        'region': zh.get('region', meta.get('region', '')),
                        'rating_name': zh.get('rating_name_zh', meta.get('ratingName', '')),
                        'size_syntax': meta.get('sizeSyntax', ''),
                        'base_material': meta.get('baseMaterial', ''),
                        'material_options': meta.get('materialOptions', []),
                        'intro_zh': zh.get('intro_zh', []),
                        'rating_note_zh': zh.get('rating_note_zh', ''),
                        'edition_note_zh': zh.get('edition_note_zh', ''),
                        'reference_doc_id': meta.get('referenceDocumentId') or '',
                        'sort_order': index,
                    },
                )
                standards[standard.id] = standard

            flange_types = {}
            for index, (slug, zh) in enumerate(zh_content.TYPES.items()):
                flange_type, _ = FlangeType.objects.update_or_create(
                    slug=slug,
                    defaults={
                        'name_zh': zh['name_zh'],
                        'name_en': zh['name_en'],
                        'note_zh': zh['note_zh'],
                        'sort_order': index,
                    },
                )
                flange_types[slug] = flange_type

            dimension_rows = 0
            for path in sorted((data_dir / 'data/dimensions').glob('*.json')):
                rows = json.loads(path.read_text('utf-8'))
                rating_cache = {}
                for row in rows:
                    standard = standards[row['std']]
                    rating_code = row['rating']
                    if rating_code not in rating_cache:
                        rating_cache[rating_code], _ = Rating.objects.update_or_create(
                            standard=standard,
                            code=rating_code,
                            defaults={
                                'label': row['rating_label'],
                                'sort_order': int(_rating_sort(rating_code)),
                            },
                        )
                    defaults = {field: row.get(field) for field in DIMENSION_FIELDS}
                    defaults.update({'size_label': row['size_label'], 'dn': row['dn']})
                    DimensionRow.objects.update_or_create(
                        standard=standard,
                        flange_type=flange_types[row['type']],
                        size=row['size'],
                        rating=rating_cache[rating_code],
                        defaults=defaults,
                    )
                    dimension_rows += 1

            reference_count = 0
            for path in sorted((data_dir / 'data/reference').glob('*.json')):
                doc = json.loads(path.read_text('utf-8'))
                verified_on = None
                if doc.get('verifiedOn'):
                    try:
                        verified_on = datetime.strptime(doc['verifiedOn'], '%Y-%m-%d').date()
                    except ValueError:
                        verified_on = None
                ReferenceDoc.objects.update_or_create(
                    id=doc['id'],
                    defaults={
                        'edition': doc.get('edition', ''),
                        'verified_on': verified_on,
                        'source_document': doc.get('sourceDocument', ''),
                        'payload': doc,
                    },
                )
                reference_count += 1

            DataImport.objects.create(
                source_version=manifest.get('version', ''),
                source_path=str(data_dir),
                dimension_rows=dimension_rows,
                standards_count=len(standards),
                reference_docs=reference_count,
                note='import_standards',
            )

        self.stdout.write(
            self.style.SUCCESS(
                f'导入完成：{len(standards)} 个标准、{dimension_rows} 行尺寸、'
                f'{reference_count} 个参考文档（数据版本 {manifest.get("version", "?")}）'
            )
        )
