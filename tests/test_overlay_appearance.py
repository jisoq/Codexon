

from cachemonitor.overlay_appearance import CodexAppearance


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
