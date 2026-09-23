"""Matched Windows font-engine samples; no app settings or taskbar changes."""
import ctypes
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontInfo
from PySide6.QtWidgets import QApplication
from cachemonitor.quick_runtime import QuickHost
from cachemonitor.presentation import Group, Column, Text, Tabs
from cachemonitor.fonts import load_bundled_fonts, configure_high_dpi

configure_high_dpi()
app = QApplication([])
load_bundled_fonts()
app.setStyle('Fusion')
font = QFont('Pretendard JP')
font.setPixelSize(14)
font.setStyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
app.setFont(font)
widget = QuickHost()
root=Group();widget.setCentralWidget(root)
widget.setStyleSheet('QWidget {font-family:"Pretendard JP";font-size:14px;font-weight:400;background:#fff;color:#263344;}')
layout = Column(root)
layout.addWidget(Text('추정 기준 · 제외 내역'))
tabs = Tabs()
tabs.addTab(Group(), '관측 구간별 환산')
tabs.addTab(Group(), '모델 · 모드 세부')
layout.addWidget(tabs)
layout.addWidget(Text('07/01 21:43 → 07/01 22:18'))
layout.addWidget(Text('주간 한도 환산 · 유효 0 / 전체 461구간'))
widget.resize(560, 190)
widget.show()

def finish():
    path = Path(sys.argv[1])
    path.parent.mkdir(parents=True, exist_ok=True)
    widget.grab().save(str(path))
    path.with_suffix('.json').write_text(json.dumps({
        'platform':os.environ.get('QT_QPA_PLATFORM', 'default'),
        'family':QFontInfo(widget.font()).family(), 'ratio':widget.devicePixelRatioF(),
        'mactype_loaded':bool(ctypes.windll.kernel32.GetModuleHandleW('MacType64.dll')),
    }, indent=2), encoding='utf-8')
    widget.release_scene()
    app.quit()

QTimer.singleShot(1000, finish)
app.exec()
