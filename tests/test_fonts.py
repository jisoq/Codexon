from PySide6.QtWidgets import QApplication

from cachemonitor.fonts import load_bundled_fonts


def test_all_bundled_weights_are_registered_with_qt():
    app = QApplication.instance() or QApplication([])
    loaded = load_bundled_fonts()
    assert set(loaded) == {'Regular', 'Medium', 'SemiBold', 'Bold'}
    assert all(families and any('Pretendard JP' in name for name in families)
               for families in loaded.values())
