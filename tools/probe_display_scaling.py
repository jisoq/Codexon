"""Verify an isolated window against each monitor's actual Windows DPI."""
import ctypes
from ctypes import wintypes as W
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
from cachemonitor.quick_runtime import QuickHost
from cachemonitor.presentation import Group, Column, Text
from cachemonitor.fonts import configure_high_dpi, configure_font_rendering, load_bundled_fonts
from cachemonitor.screens import screen_id
from cachemonitor.taskbar import NativeTaskbar


def probe():
    configure_high_dpi();configure_font_rendering()
    app=QApplication([]);load_bundled_fonts()
    native=NativeTaskbar()
    native.api.GetDpiForWindow.argtypes=[W.HWND]
    native.api.GetDpiForWindow.restype=W.UINT
    monitors={}
    callback_type=ctypes.WINFUNCTYPE(W.BOOL,W.HMONITOR,W.HDC,ctypes.POINTER(W.RECT),W.LPARAM)
    @callback_type
    def collect(handle,dc,rect,data):
        info=native.MonitorInfo();info.size=ctypes.sizeof(info)
        native.api.GetMonitorInfoW(handle,ctypes.byref(info))
        monitors[info.device]=(info.work.left,info.work.top)
        return True
    native.api.EnumDisplayMonitors(None,None,collect,0)
    window=QuickHost(None,Qt.Tool)
    root=Group();window.setCentralWidget(root)
    font=QFont('Pretendard JP');font.setPixelSize(14);window.setFont(font)
    layout=Column(root)
    layout.addWidget(Text('배율 확인 · Standard / Fast · 150% → 100%'))
    window.resize(400,200);window.show();app.processEvents()
    results=[]
    for screen in app.screens()+[app.primaryScreen()]:
        # Move the native window as Windows does during a drag. setScreen()+
        # setGeometry() races the asynchronous WM_DPICHANGED resize.
        left,top=monitors[screen_id(screen)]
        native.api.SetWindowPos(int(window.winId()),None,left+60,top+60,0,0,0x0015)
        QTest.qWait(250)
        hwnd=int(window.winId())
        dpi=native.api.GetDpiForWindow(hwnd)
        actual=native.screen_name(hwnd)
        client=native.rect(hwnd,client=True)
        row={'monitor':screen_id(screen),'actual_monitor':actual,'windows_dpi':dpi,
             'qt_scale':screen.devicePixelRatio(),'window_scale':window.devicePixelRatioF(),
             'logical_size':[window.width(),window.height()],
             'physical_client_size':[client.width(),client.height()]}
        row['passed']=(actual==screen_id(screen) and abs(screen.devicePixelRatio()-dpi/96)<1e-6
                       and abs(window.devicePixelRatioF()-dpi/96)<1e-6
                       # WM_DPICHANGED scales an integer outer frame. Nonclient
                       # rounding on a 150% <-> 100% round-trip costs 1-2 DIPs.
                       and abs(window.width()-400)<=2 and abs(window.height()-200)<=2
                       and abs(client.width()-round(window.width()*dpi/96))<=1
                       and abs(client.height()-round(window.height()*dpi/96))<=1)
        results.append(row)
    window.hide();window.release_scene();native.close()
    return {'passed':all(r['passed'] for r in results),'screens':results}


if __name__=='__main__':
    result=probe()
    if len(sys.argv)>1:
        path=Path(sys.argv[1]);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
    raise SystemExit(0 if result['passed'] else 1)
