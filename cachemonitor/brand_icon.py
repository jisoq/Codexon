"""Product artwork bundled with the application."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

_ASSETS = Path(__file__).parent / 'assets' / 'brand'

def brand_pixmap(dark=False, ratio=1):
    mode = 'dark' if dark else 'light'
    pixmap = QPixmap(str(_ASSETS / f'codexon-{mode}-ui.png'))
    if pixmap.isNull():
        raise RuntimeError('Codexon icon is missing from the distribution')
    size = round(18 * ratio)
    pixmap = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap
