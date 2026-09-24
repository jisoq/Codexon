"""Translate presentation text while keeping stored data and status keys stable."""
from functools import lru_cache
import json
import re
from pathlib import Path
from PySide6.QtCore import QObject, Property, Slot

_language = 'ko'
from .translation_catalog import CATALOG as _catalog
_parts = sorted(((source, target) for source, target in _catalog.items() if len(source) > 1),
                key=lambda item: len(item[0]), reverse=True)


class Verbatim(str):
    """Presentation content supplied by the user, never a translation key."""
    def __str__(self):return self


def set_language(value):
    global _language
    _language = 'en' if value == 'en' else 'ko'
    tr.cache_clear()


def language():
    return _language


@lru_cache(maxsize=8192, typed=True)
def tr(value):
    if isinstance(value,Verbatim):return str.__str__(value)
    if _language != 'en' or not isinstance(value, str):
        return value
    exact = _catalog.get(value)
    if exact is not None:
        return exact
    if not any('\uac00' <= char <= '\ud7a3' for char in value):
        return value
    match = re.fullmatch(r'유효\s*(\d+)\s*/\s*대상\s*(\d+)', value)
    if match:
        return f'Valid {match[1]} of {match[2]}'
    match = re.fullmatch(r'산정\s*(\d+)\s*/\s*관측\s*(\d+)', value)
    if match:
        return f'Calculated {match[1]} of {match[2]} observed'
    match = re.fullmatch(r'유효\s*([\d,]+)요청\s*·\s*포함\s*([\d,]+)호출', value)
    if match:
        return f'{match[1]} eligible requests · {match[2]} included calls'
    match = re.fullmatch(r'유효\s*([\d,]+)\s*/\s*대상\s*([\d,]+)\s*·\s*입력\s*([\d,]+)', value)
    if match:
        return f'Valid {match[1]} of {match[2]} · input {match[3]}'
    value = re.sub(r'관측\s*([\d,]+)호출\s*·\s*([\d,]+)세션',
                   lambda match: f'{match[1]} observed calls · {match[2]} sessions', value)
    rendered = value
    for source, target in _parts:
        if source in rendered:
            rendered = rendered.replace(source, target)
    return re.sub(r'(?<=\d)(?=[A-Za-z])', ' ', rendered)


def localize_state(state):
    if _language != 'en':
        return state
    result = dict(state)
    for key in ('text', 'tooltip', 'accessible', 'placeholder', 'tabTitle'):
        if key in result:
            result[key] = tr(result[key])
    if 'items' in result:
        result['items'] = [dict(item, text=tr(item.get('text', '')),
                                tooltip=tr(item.get('tooltip', ''))) for item in result['items']]
    return result


class Translator(QObject):
    @Property(str, constant=True)
    def locale(self):
        return 'en_US' if _language == 'en' else 'ko_KR'

    @Slot(str, result=str)
    def text(self, value):
        return tr(value)


class LocalizedPainter:
    """Translate labels at paint time without modifying the chart's source data."""
    def __init__(self, painter):
        self._painter = painter

    def __getattr__(self, name):
        return getattr(self._painter, name)

    def drawText(self, *args):
        if args and isinstance(args[-1], str):
            args = (*args[:-1], tr(args[-1]))
        return self._painter.drawText(*args)
