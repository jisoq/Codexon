"""Application and notification artwork."""
from functools import lru_cache
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap, QRegion
from .brand_icon import brand_pixmap


def tray_icon(rate=None, warning=False, error=False, text=None):
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    from pathlib import Path
    from .theme import shared_theme
    mode = 'dark' if shared_theme().dark else 'light'
    brand = QPixmap(str(Path(__file__).parent/'assets'/'brand'/f'codexon-{mode}-ui.png')).scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    painter.drawPixmap(2, 2, brand)
    if text is not None or error or warning:
        color = shared_theme().palette['error' if error else 'warning' if warning else 'ink']
        painter.setBrush(QColor(color));painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(29, 29, 34, 34, 10, 10)
        painter.setPen(QColor(shared_theme().readableText('surface',color)))
        label = text if text is not None else "!"
        painter.setFont(QFont("Segoe UI", 12 if len(label)>2 else 17, QFont.Bold))
        painter.drawText(29, 29, 34, 34, Qt.AlignCenter, label)
    painter.end()
    return QIcon(pixmap)


@lru_cache(maxsize=2)
def _notification_artwork(dark):
    mode = 'dark' if dark else 'light'
    source = QPixmap(str(Path(__file__).parent/'assets'/'brand'/f'codexon-{mode}-ui.png'))
    return source.copy(QRegion(source.mask()).boundingRect())


@lru_cache(maxsize=6)
def _notification_icon(dark, state):
    source = _notification_artwork(dark)
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64):
        canvas = QPixmap(size, size)
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        inset = max(1, round(size / 16))
        extent = size - 2 * inset
        logo = source.scaled(extent, extent, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter.drawPixmap((size - logo.width()) // 2, (size - logo.height()) // 2, logo)
        if state != 'normal':
            color = ('#EA9A92' if dark else '#A3261B') if state == 'error' else ('#F1BD63' if dark else '#8A5500')
            badge = max(7, round(size * .48))
            origin = size - badge
            radius = max(2, round(size * .12))
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(origin, origin, badge, badge, radius, radius)
            painter.setPen(QColor('#17191c' if dark else '#ffffff'))
            font = QFont('Segoe UI')
            font.setPixelSize(max(6, round(size * .4)))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(origin, origin, badge, badge, Qt.AlignCenter, '!')
        painter.end()
        icon.addPixmap(canvas)
    return icon


def notification_icon(warning=False, error=False):
    from .theme import shared_theme
    state = 'error' if error else 'warning' if warning else 'normal'
    return _notification_icon(shared_theme().dark, state)
