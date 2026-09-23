"""Process-local display policy and bundled font registration."""
from pathlib import Path
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication


def configure_high_dpi():
    """Before QApplication: retain 125%/150% rather than fractional scaling."""
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def configure_font_rendering():
    """Use Qt's FreeType rasterizer; avoid injected GDI glyph substitutions.

    Qt 6's Windows plugin supports fontengine=freetype. Keep explicit
    platform/engine overrides (including headless tests) untouched.
    """
    if sys.platform == 'win32':
        platform = os.environ.get('QT_QPA_PLATFORM', 'windows')
        if platform.split(':', 1)[0] == 'windows' and 'fontengine=' not in platform:
            os.environ['QT_QPA_PLATFORM'] = platform + ':fontengine=freetype'


def load_bundled_fonts(weights=('Regular', 'Medium', 'SemiBold', 'Bold')):
    folder = Path(__file__).resolve().parent / 'assets' / 'fonts'
    loaded = {}
    for weight in weights:
        path = folder / f'PretendardJP-{weight}.ttf'
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            raise RuntimeError(f'패키지 글꼴을 읽을 수 없습니다: {path.name}')
        loaded[weight] = QFontDatabase.applicationFontFamilies(font_id)
    return loaded
