"""Shared numeric tray artwork."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
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
        color = "#a57929" if error else "#b64235" if warning else "#263344"
        painter.setBrush(QColor(color));painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(29, 29, 34, 34, 10, 10)
        painter.setPen(Qt.white)
        label = text if text is not None else "!"
        painter.setFont(QFont("Segoe UI", 12 if len(label)>2 else 17, QFont.Bold))
        painter.drawText(29, 29, 34, 34, Qt.AlignCenter, label)
    painter.end()
    return QIcon(pixmap)
