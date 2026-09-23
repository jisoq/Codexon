from pathlib import Path

from PySide6.QtWidgets import QApplication

from cachemonitor.overlay import SessionOverlay
from cachemonitor.overlay_appearance import CodexAppearance, resolve_appearance


def test_codex_palette_font_size_and_explicit_dark_override_system():
    data = {'appearanceTheme': 'dark', 'sansFontSize': 16,
            'appearanceDarkChromeTheme': {'surface': '#212121', 'ink': '#eeffff',
                'accent': '#80cbc4', 'contrast': 100, 'fonts': {'ui': '"Satoshi", sans-serif'},
                'semanticColors': {'diffRemoved': '#f07178'}}}
    theme = resolve_appearance(data, False, ['Satoshi', 'Pretendard JP'])
    assert theme.dark and theme.surface == '#212121' and theme.ink == '#eeffff'
    assert theme.accent == '#80cbc4' and theme.warning == '#EDB563'
    assert theme.family == 'Satoshi' and theme.scale == 16/14
    assert resolve_appearance(data, False, ['Pretendard JP']).family == 'Pretendard JP'


def test_live_config_refresh_partial_write_and_read_only_source(tmp_path):
    path = tmp_path/'config.toml'
    first = '[desktop]\nappearanceTheme="light"\nsansFontSize=16\n[desktop.appearanceLightChromeTheme]\nsurface="#f5f3ed"\nink="#2f312d"\naccent="#3d755d"\n'
    path.write_text(first, encoding='utf-8')
    reader = CodexAppearance(path)
    a = reader.read(True)
    assert not a.dark and a.surface == '#f5f3ed' and a.accent == '#3d755d'
    assert path.read_text(encoding='utf-8') == first
    path.write_text('[desktop', encoding='utf-8')
    assert reader.read(True) == a and reader.issue
    path.write_text('[desktop]\nappearanceTheme="dark"\n', encoding='utf-8')
    assert reader.read(False).dark and not reader.issue
    path.write_text('[desktop]\nappearanceTheme="system"\n', encoding='utf-8')
    assert reader.read(True).dark and not reader.read(False).dark


def test_malformed_colors_font_and_size_have_safe_defaults():
    appearance = resolve_appearance({'sansFontSize': 400, 'appearanceLightChromeTheme': {
        'surface': 'url(http://invalid)', 'ink': None, 'contrast': -200, 'fonts': 'invalid',
        'semanticColors': 'invalid'}}, False)
    assert appearance.surface.startswith('#') and appearance.font_size == 400
    assert appearance.contrast == 0 and appearance.family == 'Pretendard JP'


def test_font_size_scales_card_and_preserves_translucent_background():
    app = QApplication.instance() or QApplication([])
    card = SessionOverlay()
    try:
        theme = resolve_appearance({'sansFontSize': 16, 'appearanceTheme': 'light',
                                   'appearanceLightChromeTheme': {'surface': '#f5f3ed', 'ink': '#2f312d'}}, False)
        card.set_content(None, appearance=theme)
        assert card.width() == round(380*16/14)
        assert card.height() == round(card.base_height()*16/14)
        from PySide6.QtTest import QTest
        card.show();QTest.qWait(30)
        image = card.grab().toImage()
        scale = image.devicePixelRatio()*theme.scale
        pixel = image.pixelColor(round(12*scale), round(90*scale))
        assert 210 < pixel.alpha() < 255
        from PySide6.QtGui import QColor
        panel = QColor(theme.panel_surface)
        assert theme.panel_surface != theme.surface
        assert abs(pixel.red()-panel.red()) <= 1 and abs(pixel.green()-panel.green()) <= 1
    finally: card.close(); app.processEvents()
