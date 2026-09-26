"""Run installation changes in the native user's context, outside MSIX overlays."""
import ctypes
import os
from pathlib import Path
import sys
import time
import uuid

from .observer_state import read_json
from .observer_task import ObserverTask


def packaged_context():
    if os.name!='nt':return False
    api=ctypes.WinDLL('kernel32').GetCurrentPackageFullName
    api.argtypes=[ctypes.POINTER(ctypes.c_uint32),ctypes.c_wchar_p]
    api.restype=ctypes.c_long
    size=ctypes.c_uint32()
    result=api(ctypes.byref(size),None)
    if result==15700:return False  # APPMODEL_ERROR_NO_PACKAGE
    if result==122:return True    # ERROR_INSUFFICIENT_BUFFER: identity exists
    raise OSError(result,'Windows package identity could not be determined')


def native_install(args):
    """Wait for the native activation receipt, never claim a queued run succeeded."""
    report=Path.home()/'.cachemonitor'/'installation-runs'/(uuid.uuid4().hex+'.json')
    report.parent.mkdir(parents=True,exist_ok=True)
    command=[sys.executable]
    if not getattr(sys,'frozen',False):command.append(str(Path(__file__).resolve().parents[1]/'recovery_main.py'))
    command+=['--native-install','--install-root',str(args.install_root),'--report',str(report)]
    if args.prepare_uninstall:
        # A one-file recovery executable has both a bootloader and Python process.
        for pid in (os.getpid(),os.getppid()):command+=['--install-caller-pid',str(pid)]
    for key in ('product_dir','language'):
        value=getattr(args,key,None)
        if value:command+=['--'+key.replace('_','-'),str(value)]
    for key in ('prepare_uninstall','isolated_install','no_launch'):
        if getattr(args,key,False):command.append('--'+key.replace('_','-'))
    task=ObserverTask(str(report),role='InstallationActivation')
    task.start(command)
    deadline=time.monotonic()+240
    while time.monotonic()<deadline:
        registration=task.inspect()
        if report.exists():
            result=read_json(report)
            if not result:raise RuntimeError('설치 결과를 읽지 못했습니다. 설치 기록을 보존합니다.')
            if not registration.get('running'):
                task.remove()
                if registration.get('last_result') != (1 if result.get('error') else 0):
                    raise RuntimeError('Windows 설치 작업의 종료 코드와 결과가 일치하지 않습니다.')
                return result
        elif (not registration.get('running') and registration.get('state') not in (2,4)
              and registration.get('last_result')!=267011):
            task.remove()
            raise RuntimeError('Windows 설치 작업이 결과 없이 종료되었습니다. 기존 설치를 확인해 주세요.')
        time.sleep(.5)
    # Keep an active installer and its journal; it may still be committing.
    raise RuntimeError('Windows 설치 작업의 완료를 아직 확인하지 못했습니다. 설치 기록을 확인한 뒤 다시 시도해 주세요.')
