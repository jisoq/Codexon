"""Measure native control occlusion and style churn on an owned test window."""
import argparse
import ctypes
from ctypes import wintypes as W
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.fonts import configure_high_dpi


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--assert-stable',action='store_true');args=parser.parse_args()
    configure_high_dpi()
    from PySide6.QtCore import QEvent,QObject,QSettings,Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget
    from cachemonitor.quick_qa import click
    from PySide6.QtQuick import QQuickItem
    from cachemonitor.overlay import OverlayController
    from cachemonitor.overlay_tracking import Selection
    from cachemonitor.overlay_windows import WindowsOverlay
    app=QApplication([])
    host=QWidget();host.setAttribute(Qt.WA_ShowWithoutActivating);host.resize(900,760);host.show()
    settings=QSettings(str(args.report.with_suffix('.ini')),QSettings.IniFormat)
    c=OverlayController(settings,native_enabled=False,appearance_path=args.report.with_suffix('.toml'))
    n=WindowsOverlay();c.native=n;n.configure(int(c.widget.winId()))
    n.visible_target=lambda hwnd:bool(n.u.IsWindowVisible(hwnd) and not n.u.IsIconic(hwnd))
    panel=int(c.widget.winId());toolbar=int(c.toolbar.winId())
    n.u.GetWindow.argtypes=[W.HWND,W.UINT];n.u.GetWindow.restype=W.HWND
    report={'refreshes':0,'style_changes':0,'toolbar_occlusions':0,'model_requests':0}
    measuring=True
    class Watch(QObject):
        def eventFilter(self,obj,event):
            if measuring and event.type()==QEvent.StyleChange:report['style_changes']+=1
            return False
    watch=Watch();c.toolbar.installEventFilter(watch)
    original=n.place
    def place(hwnd,geometry):
        result=original(hwnd,geometry)
        if measuring and hwnd==panel and c.toolbar.isVisible():
            above=n.u.GetWindow(toolbar,3)
            while above:
                if above==panel:report['toolbar_occlusions']+=1;break
                above=n.u.GetWindow(above,3)
        return result
    n.place=place
    def refresh():
        c.receive_target({'target':{'hwnd':int(host.winId())},'selection':Selection('fixture')})
        app.processEvents()
    try:
        refresh();QTest.qWait(150);refresh()
        c.toggle_opacity();QTest.qWait(70);refresh()
        report.update(style_changes=0,toolbar_occlusions=0)
        before=n.u.GetForegroundWindow()
        for _ in range(30):refresh();report['refreshes']+=1;QTest.qWait(50)
        measuring=False
        report['visible']=c.toolbar.isVisible()
        report['foreground_unchanged']=before==n.u.GetForegroundWindow()
        c.set_opacity(60);refresh()
        assert c.toolbar.slider.value()==40
        c.close_popup()
        click(c.actions,c.actions.quick.rootObject().findChild(QQuickItem,'collapse'));QTest.qWait(140);refresh();assert c.icon.isVisible()
        QTest.mouseClick(c.icon,Qt.LeftButton,pos=c.icon.rect().center());QTest.qWait(140);refresh();assert c.widget.isVisible()
        assert not c.toolbar.isVisible()
        monitor=c.monitor_geometry
        c.toggle_expanded();app.processEvents()
        expanded=c.current_geometry
        assert expanded[2]==monitor[2]+round(240*c.appearance.scale*n.u.GetDpiForWindow(int(host.winId()))/96)
        detail_frames=[]
        for _ in range(12):
            QTest.qWait(20)
            detail_frames.append(c.current_geometry)
            assert c.widget.isVisible() and c.detail.isVisible() and c.monitor_geometry==monitor
            assert c.detail.quick.rootObject().opacity()==1
        assert all(box==expanded for box in detail_frames)
        c.toggle_expanded();app.processEvents()
        assert c.current_geometry==monitor and not c.detail.isVisible()
        report['detail_stable_frames']=len(detail_frames)
        report['controls_work']=True
        if args.assert_stable:
            assert report['style_changes']==0 and report['toolbar_occlusions']==0,report
            assert report['visible'] and report['foreground_unchanged']
    finally:
        c.stop();host.close();app.processEvents()
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report))


if __name__=='__main__':main()
