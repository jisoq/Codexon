"""Accent collisions, shared token meaning, and actual theme-selection UI."""
from dataclasses import replace
from itertools import combinations

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.overlay_appearance import default_appearance, CodexAppearance
from cachemonitor.overlay_view import palette
from cachemonitor.token_colors import TOKEN_COLORS, token_palette, color_distance, contrast_ratio


@pytest.mark.parametrize('dark', [False, True])
@pytest.mark.parametrize('surface', [None, '#808080', '#f6f4ed', '#212121'])
def test_token_colors_separate_accent_and_each_other_on_custom_surfaces(dark, surface):
    for accent in (*TOKEN_COLORS[dark].values(), '#ffffff', '#000000', '#ff0000', '#0000ff'):
        appearance = replace(default_appearance(dark), accent=accent)
        if surface:
            appearance = replace(appearance, surface=surface)
        colors = token_palette(appearance)
        assert token_palette(appearance) == colors
        assert min(color_distance(a, b) for a, b in combinations(colors.values(), 2)) >= .09
        assert all(contrast_ratio(c, appearance.surface) >= 3 for c in colors.values())
        rendered = palette(appearance)
        assert all(rendered[k].name() == c for k, c in colors.items())
        assert contrast_ratio(rendered['cached_text'], appearance.surface) >= 4.5


def test_token_charts_use_distinct_semantics():
    from cachemonitor.charts import TokenComposition
    app = QApplication.instance() or QApplication([])
    chart = TokenComposition('input')
    chart.set_parts(dict(total=10, ordinary=4, read=3, write=2, unclassified=1))
    assert [r['color'] for r in chart.rows] == ['uncached', 'cached', 'written', 'unknown']
    chart = TokenComposition('output')
    chart.set_parts(dict(total=10, ordinary=5, reasoning=4, unclassified=1))
    assert [r['color'] for r in chart.rows] == ['output', 'reasoning', 'unknown']


def test_quick_theme_selection_updates_overlay_and_preserves_state(tmp_path):
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.analysis_worker import AnalysisBridge
    from cachemonitor.overlay import install_overlay
    from cachemonitor.overlay_tracking import Selection
    from cachemonitor.quick_qa import click, control
    from cachemonitor.theme import shared_theme
    from test_overlay_layout_controller import Native
    from test_overlay_presentation import summary

    app = QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration', True)
    settings = QSettings(str(tmp_path/'settings.ini'), QSettings.IniFormat)
    settings.setValue('overlay/theme', 'dark')  # Legacy setting must not override current selection.
    settings.setValue('ui/theme', 'light')
    config = tmp_path/'config.toml'
    def write_config(dark, accent):
        mode = 'dark' if dark else 'light'
        config.write_text(f'[desktop]\nappearanceTheme="{mode}"\n'
                          f'[desktop.appearance{mode.title()}ChromeTheme]\naccent="{accent}"\n', encoding='utf-8')
    write_config(True, '#79c4a5')
    theme = shared_theme(); previous_reader = theme._reader
    theme._reader = CodexAppearance(config)
    window = Dashboard([], start_worker=False, live_limits=False, manage_observer=False, settings=settings)
    window.worker = AnalysisBridge([], static_snapshot={})
    controller = install_overlay(window, native_enabled=False)
    controller.appearance_reader = CodexAppearance(config)
    controller.native = Native()
    controller.receive_snapshot({'overlay_sessions': [summary()]})
    def refresh():
        controller.receive_target(dict(target={'hwnd': 1}, selection=Selection('clean')))
        QTest.qWait(80)
    try:
        window.tick.stop(); window.show(); window.open_settings(); QTest.qWait(100)
        refresh(); controller.toggle_expanded(); refresh()
        model = controller.widget.content_model
        model.select(model.call_id(model.rows()[0]))
        selected = model.selected_id
        anchor = controller.anchor
        choice = window.settings_page.controls['theme']
        item = control(window, choice)
        for index, mode in ((2, 'dark'), (1, 'light'), (0, 'codex')):
            click(window, item)
            QTest.keyClick(window.quick, Qt.Key_Home)
            for _ in range(index): QTest.keyClick(window.quick, Qt.Key_Down)
            QTest.keyClick(window.quick, Qt.Key_Return)
            refresh()
            assert settings.value('ui/theme') == mode
            assert controller.dark == theme.dark == (mode != 'light')
            colors = palette(controller.appearance)
            assert all(theme.palette[k] == colors[k].name() for k in ('accent', *TOKEN_COLORS[False]))
            assert controller.expanded and controller.anchor == anchor and model.selected_id == selected
            assert controller.widget.grab().save(str(tmp_path/f'overlay-{mode}.png'))
            assert window.grab().save(str(tmp_path/f'dashboard-{mode}.png'))
        # A Codex appearance change must propagate without resetting the selection.
        write_config(False, '#765aa3')
        theme.configure('codex'); controller.next_theme = 0; refresh()
        assert not controller.dark and not theme.dark
        assert model.selected_id == selected and controller.expanded
        assert all(theme.palette[k] == palette(controller.appearance)[k].name() for k in TOKEN_COLORS[False])
        assert settings.value('overlay/theme') == 'dark'
        assert not window.qml_errors and not controller.widget.qml_errors
        assert all(not chrome.qml_errors for chrome in controller.chrome)
    finally:
        controller.stop(); window.quit_app(); theme._reader = previous_reader; app.processEvents()
