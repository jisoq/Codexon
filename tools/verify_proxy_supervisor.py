"""Isolated Task Scheduler + real TCP + real SQLite fault verification. No model API calls."""
import argparse
import ctypes
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_control import ObserverManager,atomic_write
from cachemonitor.observer_task import ObserverTask
from cachemonitor.model_evidence import home_key
from cachemonitor.observer_state import read_json


def wait_until(check,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=check()
        if value:return value
        time.sleep(.2)
    raise AssertionError('Verification timed out')


def stop_owned_test_process(pid):
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_bool,ctypes.c_ulong];k.OpenProcess.restype=ctypes.c_void_p
    k.TerminateProcess.argtypes=[ctypes.c_void_p,ctypes.c_uint];k.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=k.OpenProcess(1,False,int(pid))
    if handle:
        try:assert k.TerminateProcess(handle,42)
        finally:k.CloseHandle(handle)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scenario',choices=('storage','exit','runtime-lock'),required=True)
    parser.add_argument('--exe',type=Path);args=parser.parse_args()
    root=args.output.resolve();workspace=Path(__file__).resolve().parents[1]
    if not root.is_relative_to(workspace/'artifacts'):parser.error('Output must stay in artifacts')
    root.mkdir(parents=True,exist_ok=True)
    fixture=root/('fixture-'+uuid.uuid4().hex[:8]);home=fixture/'home';home.mkdir(parents=True)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port!=8768
    manager=ObserverManager(home,fixture/'data',url=f'http://127.0.0.1:{port}')
    before='# fixture configuration\nmodel="fixture"\nopenai_base_url="https://api.openai.com/v1"\n'
    manager.config_path.write_text(before,encoding='utf-8')
    manager.set_url(manager.url)
    manager.write_state({'home':home_key(home),'enabled':True,'phase':'active','upstream':'openai',
                         'previous_url':'https://api.openai.com/v1'})
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
            calls.append(data)
            status=data.get('http_status',200)
            result={'object':'response','id':'fixture-'+str(len(calls)),'model':'fixture','status':'completed'}
            payload=json.dumps(result).encode()
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    command=manager.supervisor_command('openai')+['--upstream-url',f'http://127.0.0.1:{server.server_port}']
    if args.exe:
        command=[str(args.exe.resolve()),*command[command.index('--proxy-supervisor'):]]
    report={'scenario':args.scenario,'fixture':str(fixture),'model_requests':0,'port':port}
    worker=None;blocker=None
    def health():
        try:return manager.health(timeout=.3)
        except (OSError,RuntimeError):return None
    def request(status=200):
        data=json.dumps({'model':'fixture','http_status':status}).encode()
        req=urllib.request.Request(manager.url+'/responses',data=data,headers={'Content-Type':'application/json'})
        try:response=urllib.request.urlopen(req,timeout=5)
        except urllib.error.HTTPError as exc:response=exc
        assert response.status==status
        return json.loads(response.read())
    try:
        manager.task.start(command,autostart=True)
        h=wait_until(health);worker=h['pid']
        runtime=wait_until(lambda:manager.runtime() if manager.runtime().get('worker_pid')==worker else None)
        assert runtime['worker_pid']==worker and runtime['pid']!=worker
        info=manager.task.inspect();assert info['autostart'] and '--proxy-supervisor' in info['arguments']
        parent=subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(runtime["pid"])}").ParentProcessId'],
            creationflags=subprocess.CREATE_NO_WINDOW,text=True).strip()
        assert int(parent)!=__import__('os').getpid()
        report.update(supervisor_pid=runtime['pid'],worker_pid=worker,parent_pid=int(parent),autostart=True)
        for status in (401,429,503,200):request(status)
        assert manager.state()['enabled'] and health()['internal_failure_streak']==0
        report['upstream_errors_did_not_stop_proxy']=True
        if args.scenario=='runtime-lock':
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_void_p,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_void_p]
            kernel.CreateFileW.restype=ctypes.c_void_p
            kernel.CloseHandle.argtypes=[ctypes.c_void_p]
            handle=kernel.CreateFileW(str(manager.runtime_path),0x80000000,3,None,3,0,None)
            assert handle!=ctypes.c_void_p(-1).value,ctypes.get_last_error()
            try:
                time.sleep(3)
                assert health()['pid']==worker and manager.state()['enabled']
            finally:kernel.CloseHandle(handle)
            recovered=wait_until(lambda: (value if value.get('publication_failures',0)>0 else None)
                                 if (value:=manager.runtime()) else None,8)
            assert recovered['pid']==runtime['pid'] and recovered['worker_pid']==worker
            report.update(runtime_lock_survived=True,publication_failures=recovered['publication_failures'])
        if args.scenario=='storage':
            blocker=sqlite3.connect(manager.evidence,timeout=.2);blocker.execute('BEGIN IMMEDIATE')
            for _ in range(3):request()
            assert health()['storage_failure_streak']>=3
        else:
            # This PID came from the identity-checked isolated port, never port 8768.
            assert health()['pid']==worker and manager.runtime()['worker_pid']==worker
            stop_owned_test_process(worker)
        incident=wait_until(lambda:manager.state().get('incident'),25)
        assert not manager.state()['enabled']
        assert manager.config()[1]['openai_base_url']=='https://api.openai.com/v1'
        assert '# fixture configuration' in manager.config_path.read_text()
        wait_until(lambda:read_json(manager.runtime_path).get('phase')=='stopped',15)
        assert not manager.task.inspect()['registered']
        assert len(calls)==(7 if args.scenario=='storage' else 4)
        report.update(direct_restored=True,autostart_removed=True,worker_stopped=True,
                      forwarded_requests=len(calls),no_replay=True,incident=incident)
    finally:
        if blocker:blocker.rollback();blocker.close()
        runtime=read_json(manager.runtime_path)
        if worker and health():
            control=Path(runtime.get('control_file',''))
            if control.is_relative_to(fixture/'data'):
                atomic_write(control,json.dumps({'id':health().get('control_id'),'action':'drain'}).encode())
        manager.task.remove()
        server.shutdown();server.server_close();thread.join()
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
