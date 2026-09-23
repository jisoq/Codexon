"""Noninteractive packaged Qt diagnostic; does not start services or a GUI."""
import ctypes
import json
import os
from pathlib import Path
import sys
import traceback


def modules():
    if os.name!='nt':return []
    from ctypes import wintypes as W
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    psapi=ctypes.WinDLL('psapi',use_last_error=True)
    api.GetCurrentProcess.restype=W.HANDLE
    psapi.EnumProcessModules.argtypes=[W.HANDLE,ctypes.POINTER(W.HMODULE),W.DWORD,ctypes.POINTER(W.DWORD)]
    psapi.GetModuleFileNameExW.argtypes=[W.HANDLE,W.HMODULE,W.LPWSTR,W.DWORD]
    process=api.GetCurrentProcess();handles=(W.HMODULE*2048)();needed=W.DWORD()
    if not psapi.EnumProcessModules(process,handles,ctypes.sizeof(handles),ctypes.byref(needed)):return []
    paths=[]
    for handle in handles[:needed.value//ctypes.sizeof(W.HMODULE)]:
        path=ctypes.create_unicode_buffer(32768)
        if psapi.GetModuleFileNameExW(process,handle,path,len(path)):
            if any(word in Path(path.value).name.lower() for word in ('qt','pyside','shiboken','msvcp','vcruntime','mactype','python','icu')):paths.append(path.value)
    return paths


def main(output):
    from .version import VERSION, PROXY_VERSION
    report={'executable':sys.executable,'version':VERSION,'proxy_version':PROXY_VERSION,
            'before':modules(),'errors':[]}
    try:
        from PySide6 import QtCore,QtGui,QtWidgets,QtQml,QtQuick,QtQuickWidgets
        report.update(pyside=QtCore.__version__,qt=QtCore.qVersion())
    except Exception:
        report['errors'].append(traceback.format_exc())
    report['after']=modules()
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 1 if report['errors'] else 0
