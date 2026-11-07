"""Small helpers for the bilingual / and /en/ site."""

from django.utils import translation


def current_lang():
    return 'en' if translation.get_language().startswith('en') else 'zh'


def is_en():
    return current_lang() == 'en'


def render_page(request, template_name, context=None):
    prefix = 'en/' if is_en() else ''
    from django.shortcuts import render
    return render(request, prefix + template_name, context or {})
