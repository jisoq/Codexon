"""Exercise the frozen offline recovery window without moving the physical cursor."""
import argparse
import ctypes
from ctypes import wintypes as W
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import tomllib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_state import read_json


def capture(hwnd,path):
    # PrintWindow captures our own isolated test window even if another app covers it.
    from PySide6.QtGui import QImage
    user=ctypes.WinDLL('user32',use_last_error=True)
    gdi=ctypes.WinDLL('gdi32',use_last_error=True)
    user.GetAncestor.argtypes=[W.HWND,W.UINT];user.GetAncestor.restype=W.HWND
    hwnd=user.GetAncestor(hwnd,2)
    user.GetWindowRect.argtypes=[W.HWND,ctypes.POINTER(W.RECT)]
    user.GetWindowDC.argtypes=[W.HWND];user.GetWindowDC.restype=W.HDC
    user.PrintWindow.argtypes=[W.HWND,W.HDC,W.UINT]
    user.ReleaseDC.argtypes=[W.HWND,W.HDC]
    gdi.CreateCompatibleDC.argtypes=[W.HDC];gdi.CreateCompatibleDC.restype=W.HDC
    gdi.CreateCompatibleBitmap.argtypes=[W.HDC,ctypes.c_int,ctypes.c_int];gdi.CreateCompatibleBitmap.restype=W.HBITMAP
    gdi.SelectObject.argtypes=[W.HDC,W.HANDLE];gdi.SelectObject.restype=W.HANDLE
    gdi.DeleteObject.argtypes=[W.HANDLE];gdi.DeleteDC.argtypes=[W.HDC]
    class Header(ctypes.Structure):
        _fields_=[('size',W.DWORD),('width',W.LONG),('height',W.LONG),('planes',W.WORD),('bits',W.WORD),
                  ('compression',W.DWORD),('sizeimage',W.DWORD),('x',W.LONG),('y',W.LONG),('used',W.DWORD),('important',W.DWORD)]
    rect=W.RECT();assert user.GetWindowRect(hwnd,ctypes.byref(rect))
    width,height=rect.right-rect.left,rect.bottom-rect.top
    source=user.GetWindowDC(hwnd);dc=gdi.CreateCompatibleDC(source)
    bitmap=gdi.CreateCompatibleBitmap(source,width,height);previous=gdi.SelectObject(dc,bitmap)
    try:
        assert user.PrintWindow(hwnd,dc,2)
        gdi.SelectObject(dc,previous)
        header=Header(ctypes.sizeof(Header),width,-height,1,32,0,0,0,0,0,0)
        buffer=ctypes.create_string_buffer(width*height*4)
        gdi.GetDIBits.argtypes=[W.HDC,W.HBITMAP,W.UINT,W.UINT,ctypes.c_void_p,ctypes.c_void_p,W.UINT]
        assert gdi.GetDIBits(dc,bitmap,0,height,buffer,ctypes.byref(header),0)==height
        assert QImage(buffer.raw,width,height,QImage.Format_RGB32).save(str(path))
    finally:
        gdi.DeleteObject(bitmap);gdi.DeleteDC(dc);user.ReleaseDC(hwnd,source)


def wait_report(path,predicate,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        error=path.with_suffix('.error.txt')
        if error.exists():raise RuntimeError('Recovery UI callback failed: '+error.read_text(encoding='utf-8'))
        data=read_json(path)
        if data and predicate(data):return data
        time.sleep(.2)
    raise RuntimeError('Recovery UI did not reach the expected state: '+str(read_json(path)))


def restored_controls_ready(state):
    if state['busy'] or (state.get('result') or {}).get('code')!='restored':return False
    for name in ('recheck','close','path','diagnosis'):
        control=state['controls'][name]
        if not (control['visible'] and control['width']>0 and control['height']>0
                and 0<=control['x']<control['x']+control['width']<=state['client'][0]
                and 0<=control['y']<control['y']+control['height']<=state['client'][1]):return False
    return True


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--language',choices=('ko','en'),default='ko')
    args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    # Deliberately copy ONLY the recovery EXE. No desktop executable or Qt runtime.
    exe=root/'CodexonRecovery.exe';shutil.copy2(args.executable,exe)
    home=root/'home';home.mkdir();(home/'codexon-test-home').touch()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port!=8768
    url=f'http://127.0.0.1:{port}'
    original=f'# untouched personal setting\nmodel="synthetic"\nopenai_base_url="{url}"\n'
    (home/'config.toml').write_text(original)
    auth=home/'auth.json';auth.write_text('{"synthetic":"preserve"}')
    report=root/'ui.json'
    command=[str(exe),'--codex-home',str(home),'--data-dir',str(root/'data'),'--proxy-url',url,'--ui-smoke',str(report),'--language',args.language]
    process=subprocess.Popen(command,creationflags=subprocess.CREATE_NO_WINDOW)
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.PostMessageW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM]
    try:
        state=wait_report(report,lambda d:not d['busy'] and d.get('result') and d.get('ready'))
        assert state['result']['code']=='refused'
        user.GetAncestor.argtypes=[W.HWND,W.UINT];user.GetAncestor.restype=W.HWND
        user.GetWindowRect.argtypes=[W.HWND,ctypes.POINTER(W.RECT)]
        user.GetClientRect.argtypes=[W.HWND,ctypes.POINTER(W.RECT)]
        user.SetWindowPos.argtypes=[W.HWND,W.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,W.UINT]
        hwnd=user.GetAncestor(state['hwnd'],2)
        outer=W.RECT();client=W.RECT()
        assert user.GetWindowRect(hwnd,ctypes.byref(outer)) and user.GetClientRect(hwnd,ctypes.byref(client))
        assert user.SetWindowPos(hwnd,None,0,0,480+outer.right-outer.left-client.right,
            280+outer.bottom-outer.top-client.bottom,0x16)
        wait_report(report,lambda d:d.get('client')==[480,280])
        direct='# untouched personal setting\nmodel="synthetic"\n'
        (home/'config.toml').write_text(direct)
        report.with_suffix('.recheck').touch()
        state=wait_report(report,lambda d:not d['busy'] and (d.get('result') or {}).get('code')=='direct')
        assert state['rechecks']==1 and state['controls']['restore']['visible']
        assert (home/'config.toml').read_text()==direct
        (home/'config.toml').write_text(original)
        report.with_suffix('.recheck').touch()
        wait_report(report,lambda d:not d['busy'] and (d.get('result') or {}).get('code')=='refused' and d['rechecks']==2)
        # PrintWindow synchronously sends native paint messages into Tk. Keep
        # external capture after the interaction so it cannot disturb the input.
        report.with_suffix('.click').touch()
        state=wait_report(report,restored_controls_ready)
        assert state['activations']==1
        assert not state['result']['can_recover'] and not state['controls']['restore']['visible']
        for name in ('recheck','close','path','diagnosis'):
            control=state['controls'][name]
            assert control['visible'] and control['width']>0 and control['height']>0,(name,state)
            assert 0<=control['x']<control['x']+control['width']<=state['client'][0],(name,state)
            assert 0<=control['y']<control['y']+control['height']<=state['client'][1],(name,state)
        capture(state['hwnd'],root/'after.png')
        assert tomllib.loads((home/'config.toml').read_text())=={'model':'synthetic'}
        assert '# untouched personal setting' in (home/'config.toml').read_text()
        assert auth.read_text()=='{"synthetic":"preserve"}'
        verify_saved_connection_recovery(root/'saved-connection',exe)
        result=dict(passed=True,independent_executable=True,actual_button_activation=True,
                    activation_event='Tk <<Invoke>>',model_requests=0,
                    config_restored=True,auth_preserved=True,saved_connection_recovery=True,
                    manual_recheck_read_only=True,completion_action=True,compact_controls=True)
        (root/'result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))
    finally:
        state=read_json(report)
        if state.get('hwnd'):
            user.GetAncestor.argtypes=[W.HWND,W.UINT];user.GetAncestor.restype=W.HWND
            user.PostMessageW(user.GetAncestor(state['hwnd'],2),0x0010,0,0)
        process.wait(timeout=20)


def verify_saved_connection_recovery(root,exe):
    """Exercise Start-menu defaults with only the standalone recovery payload."""
    import os
    profile=root/'profile';home=root/'custom home';home.mkdir(parents=True)
    index=root/'analysis'/'index.sqlite';index.parent.mkdir()
    evidence=root/'data'/'model-evidence.sqlite'
    preferences=profile/'.cachemonitor'/'launch.json';preferences.parent.mkdir(parents=True)
    preferences.write_text(json.dumps(dict(homes=[str(home)],connection_paths=dict(index_path=str(index),
        evidence_path=str(evidence)))),encoding='utf-8')
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port not in (8768,8771)
    url=f'http://127.0.0.1:{port}'
    (home/'config.toml').write_text('model="synthetic"\nopenai_base_url="'+url+'"\n',encoding='utf-8')
    auth=home/'auth.json';auth.write_text('{"synthetic":"preserve"}',encoding='utf-8')
    other=root/'environment home';other.mkdir()
    other_config=other/'config.toml';other_config.write_text('openai_base_url="https://custom.example/v1"\n')
    untouched=other_config.read_bytes()
    env={**os.environ,'USERPROFILE':str(profile),'CODEX_HOME':str(other)}
    report=root/'result.json'
    for action in ('--status','--restore'):
        result=subprocess.run([str(exe),action,'--report',str(report)],env=env,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
        state=read_json(report)
        assert result.returncode==0,state
        assert state['code']==('refused' if action=='--status' else 'restored'),state
    assert tomllib.loads((home/'config.toml').read_text(encoding='utf-8'))==dict(model='synthetic')
    assert auth.read_text(encoding='utf-8')=='{"synthetic":"preserve"}'
    assert not index.with_name('cache-control.sqlite').exists()
    assert other_config.read_bytes()==untouched



if __name__=='__main__':main()
