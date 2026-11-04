"""基础回归测试：导入、查询解析、页面、打印、p/T、SVG。"""

import json

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from catalog import pt
from catalog.drawings import flange_svg
from catalog.models import DimensionRow, ReferenceDoc, Standard


class ImportedDataTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('import_standards', verbosity=0)

    def test_counts(self):
        self.assertEqual(Standard.objects.count(), 5)
        self.assertEqual(DimensionRow.objects.count(), 1348)
        self.assertEqual(ReferenceDoc.objects.count(), 4)

    def test_search_parsing(self):
        from catalog import search

        queries = {
            'DN100 PN16': 3,
            '6″ Class 150': 3,
            'B16.5 4 寸 300 磅': 3,
            '100A 10K 滑套': 1,
        }
        for query, expected in queries.items():
            queryset, parsed = search.search(query)
            self.assertEqual(queryset.count(), expected, msg=f'{query} -> {parsed.chips}')
            self.assertTrue(parsed.chips)

    def test_public_pages(self):
        row = DimensionRow.objects.get(
            standard_id='asme-b16-5', flange_type_id='weld-neck', size='6', rating__code='150'
        )
        for url in [
            reverse('catalog:home'),
            reverse('catalog:standard_detail', args=['asme-b16-5']),
            reverse('catalog:spec_detail', args=[row.pk]),
            reverse('catalog:spec_print', args=[row.pk]),
            reverse('catalog:compare') + '?dn=100',
            reverse('catalog:reference_topic', args=['asme-b16-5', 'pressure-ratings']),
            reverse('catalog:healthz'),
        ]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, msg=url)

    def test_print_page_has_svg_and_source(self):
        row = DimensionRow.objects.get(
            standard_id='en-1092-1', flange_type_id='weld-neck', size='dn100', rating__code='pn16'
        )
        response = self.client.get(reverse('catalog:spec_print', args=[row.pk]))
        self.assertContains(response, '<svg')
        self.assertContains(response, row.standard.edition)

    def test_pressure_temperature_series(self):
        def series(std_id, code, label):
            standard = Standard.objects.get(pk=std_id)
            doc = ReferenceDoc.objects.get(pk=standard.reference_doc_id)
            return pt.rating_series(doc, std_id, code, label)

        asme = series('asme-b16-5', '150', 'Class 150')
        self.assertTrue(asme['available'])
        self.assertEqual(asme['rows'][0][1], '19.6')
        self.assertEqual(asme['unit'], 'bar')

        en = series('en-1092-1', 'pn16', 'PN16')
        self.assertTrue(en['available'])
        self.assertEqual(en['rows'][0][1], '16.0')

        jis = series('jis-b2220', '10k', '10K')
        self.assertTrue(jis['available'])
        self.assertEqual(jis['unit'], 'MPa')

    def test_admin_requires_login(self):
        response = self.client.get('/admin/catalog/dimensionrow/')
        self.assertEqual(response.status_code, 302)


class DrawingTest(SimpleTestCase):
    def _rows(self):
        directory = settings.DATA_DIR / 'data/dimensions'
        rows = []
        for filename, type_slug, size, rating in [
            ('asme-b16-5.json', 'weld-neck', '6', '150'),
            ('asme-b16-5.json', 'slip-on', '4', '300'),
            ('asme-b16-5.json', 'blind', '8', '150'),
            ('en-1092-1.json', 'weld-neck', 'dn100', 'pn16'),
            ('jis-b2220.json', 'slip-on', '100a', '10k'),
            ('asme-b16-47-a.json', 'blind', '36', '900'),
        ]:
            data = json.loads((directory / filename).read_text('utf-8'))
            rows.append(next(
                row for row in data
                if row['type'] == type_slug and row['size'] == size and row['rating'] == rating
            ))
        return rows

    def test_svg_is_valid_and_finite(self):
        for row in self._rows():
            for width, height in [(170, 105), (116, 74)]:
                svg = flange_svg(row, lang='zh', width_mm=width, height_mm=height)
                self.assertIn('<svg', svg)
                self.assertNotIn('nan', svg.lower())
                self.assertNotIn('infinity', svg.lower())
