"""Interactive overlay preview with isolated settings and synthetic usage."""
from pathlib import Path
import sys,tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from demo_speed_overlay import sample
from cachemonitor.fonts import configure_font_rendering,configure_high_dpi,load_bundled_fonts
configure_font_rendering();configure_high_dpi()
from PySide6.QtCore import QSettings,QTimer
from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLabel,QPushButton,QComboBox
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_windows import WindowsOverlay
from cachemonitor.overlay_tracking import Selection
from cachemonitor.i18n import set_language
app=QApplication([]);load_bundled_fonts()
temporary=tempfile.TemporaryDirectory(prefix='codexon-overlay-preview-')
settings=QSettings(str(Path(temporary.name)/'settings.ini'),QSettings.IniFormat)
settings.setValue('ui/theme','dark')
host=QWidget();host.setWindowTitle('Codexon Overlay Preview')
layout=QVBoxLayout(host);layout.setContentsMargins(24,24,680,24)
layout.addWidget(QLabel('오버레이 미리보기 / Overlay Preview'))
language=QComboBox();language.addItems(['한국어','English']);layout.addWidget(language)
theme=QComboBox();theme.addItems(['Dark','Light']);layout.addWidget(theme)
scenario=QComboBox();scenario.addItems(['모델 일치 / Match','모델 불일치 / Mismatch','확인 불가 / Unknown','최근 캐시 0% / Cache miss']);layout.addWidget(scenario)
layout.addStretch();close=QPushButton('닫기 / Close');layout.addWidget(close);close.clicked.connect(host.close)
host.resize(1100,820);area=app.primaryScreen().availableGeometry();host.resize(min(host.width(),area.width()-48),min(host.height(),area.height()-48));host.move(area.center()-host.rect().center());host.show()
controller=None;data=None
native=WindowsOverlay();hwnd=int(host.winId());native.visible_target=lambda h:h==hwnd and bool(native.u.IsWindowVisible(h) and not native.u.IsIconic(h))
def poll():
    if controller:
        controller.receive_snapshot({'overlay_sessions':[data]})
        controller.receive_target({'target':{'hwnd':hwnd},'selection':Selection('speed-demo')})
def rebuild(*args):
    global controller,data
    if controller:controller.stop()
    set_language('en' if language.currentIndex() else 'ko')
    settings.setValue('ui/theme','light' if theme.currentIndex() else 'dark')
    data=sample('normal');data['title']='Overlay preview' if language.currentIndex() else '오버레이 미리보기'
    row=data['latest'];row['model_alert_confirmed']=False
    if scenario.currentIndex()==1:row.update(response_model='gpt-6-sol',model_match='불일치',model_alert_confirmed=True)
    elif scenario.currentIndex()==2:row.update(response_model=None,model_match='미확인')
    elif scenario.currentIndex()==3:row.update(cache_rate=0,cached=0)
    data['recent'][-1].update(row)
    controller=OverlayController(settings,native_enabled=False);controller.native=native;poll()
for control in (language,theme,scenario):control.currentIndexChanged.connect(rebuild)
rebuild();timer=QTimer();timer.timeout.connect(poll);timer.start(500)
def cleanup():
    timer.stop()
    if controller:controller.stop()
app.aboutToQuit.connect(cleanup)
raise SystemExit(app.exec())
