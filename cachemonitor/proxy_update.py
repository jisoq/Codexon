"""Independent, same-address proxy replacement with persistent rollback metadata."""
import argparse
import ctypes
import json
import subprocess
import sys
import time
from pathlib import Path

from .observer_control import ObserverManager, atomic_write
from .observer_state import ProcessLock, read_json
from .version import VERSION, PROXY_VERSION

BUSY = ('queued', 'waiting', 'switching', 'rollback')


def process_executable(pid):
    from ctypes import wintypes as W
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    api.OpenProcess.restype = W.HANDLE
    api.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    api.CloseHandle.argtypes = [W.HANDLE]
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle: raise OSError('프록시 실행 파일을 확인하지 못했습니다.')
    try:
        size = W.DWORD(32768); value = ctypes.create_unicode_buffer(size.value)
        if not api.QueryFullProcessImageNameW(handle, 0, value, ctypes.byref(size)):
            raise OSError('프록시 실행 경로를 읽지 못했습니다.')
        return value.value
    finally: api.CloseHandle(handle)


class ProxyUpdate:
    def __init__(self, manager, clock=time.monotonic, sleep=time.sleep):
        self.manager, self.clock, self.sleep = manager, clock, sleep
        self.path = manager.directory/'proxy-update.json'

    def publish(self, phase, **extra):
        with ProcessLock(self.manager.directory/'proxy-update-journal.lock',timeout=5):
            data = read_json(self.path)
            data.update(phase=phase, updated_at=time.time(), target_version=PROXY_VERSION, **extra)
            atomic_write(self.path, json.dumps(data, ensure_ascii=False).encode())

    def ready(self, version, timeout=30):
        deadline = self.clock()+timeout
        while self.clock()<deadline:
            health = self.manager.health(timeout=1)
            if (health and health.get('version')==version and health.get('status')=='ok'
                    and not health.get('draining')
                    and self.manager.runtime().get('phase') in ('ready','active')):
                return
            self.sleep(.5)
        raise RuntimeError('새 프록시의 정상 실행을 확인하지 못했습니다.')

    def rollback(self, data):
        self.publish('rollback')
        self.drain()
        self.manager.task.stop()
        self.await_supervisor_exit()
        self.manager.task.start(data['rollback_command'], autostart=True)
        self.ready(data['previous_version'])
        self.publish('failed', message='업데이트 실패 · 이전 버전으로 복구했습니다.')

    def await_supervisor_exit(self):
        with ProcessLock(self.manager.directory/'proxy-supervisor.lock', timeout=30):
            pass

    def drain(self):
        m=self.manager
        health=m.health(timeout=3)
        if health:
            if not health.get('control_id'):raise RuntimeError('기존 연결을 안전하게 종료할 수 없습니다.')
            control=m.directory/('proxy-control-'+health['control_id']+'.json')
            atomic_write(control,json.dumps({'action':'drain','id':health['control_id']}).encode())
        while True:
            current=m.health(timeout=3)
            if current is None and m.health_state=='refused':return
            # A request that raced the idle check must be allowed to finish.
            self.publish(read_json(self.path).get('phase','switching'));self.sleep(.5)

    def preflight(self):
        report=self.manager.directory/'proxy-update-runtime.json'
        result=subprocess.run([sys.executable,'--verify-runtime',str(report)],timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        value=read_json(report)
        if (result.returncode or value.get('version')!=VERSION
                or value.get('proxy_version')!=PROXY_VERSION or value.get('errors')!=[]):
            raise RuntimeError('새 배포본 실행 검사 실패 · 기존 프록시를 유지합니다.')

    def run(self):
        m = self.manager
        with ProcessLock(m.directory/'proxy-update.lock'):
            data = read_json(self.path)
            # A killed updater is restarted by Task Scheduler using this journal.
            if data.get('phase') in ('switching','rollback'):
                if not m.state().get('enabled') or m.config()[1].get('openai_base_url')!=m.url:
                    self.publish('cancelled',message='프록시가 꺼져 복구 예약을 취소했습니다.');return
                try:
                    with ProcessLock(m.control_lock, timeout=30):
                        self.rollback(data)
                except Exception as exc:
                    self.publish('rollback',message='복구 재시도 필요: '+str(exc))
                    raise
                return
            try:
                self.preflight()
                health = m.health(timeout=3)
                if not health or not health.get('control_id'):
                    raise RuntimeError('현재 프록시에서 안전한 업데이트를 지원하지 않습니다.')
                runtime = m.runtime()
                old_exe = process_executable(runtime['pid'])
                upstream = m.state().get('upstream') or m.upstream()
                command = m.supervisor_command(upstream)
                if (health.get('version')==PROXY_VERSION and health.get('lifecycle')=='managed'
                        and Path(old_exe).resolve()==Path(command[0]).resolve()):
                    self.publish('complete', message='최신 버전입니다.'); return
                if Path(old_exe).name.lower() not in ('cachemonitor.exe','codexon.exe'):
                    raise RuntimeError('배포 실행 파일에서 업데이트하세요.')
                rollback = [old_exe, *command[1:]]
                if health.get('lifecycle')!='managed':
                    # Old binaries do not understand --managed; retain their launcher protocol.
                    rollback = [value for value in rollback if value!='--managed']
                    if '--model-proxy' in rollback:
                        rollback[rollback.index('--model-proxy')]='--proxy-supervisor'
                self.publish('waiting', rollback_command=rollback,
                             previous_version=health['version'], message='기존 연결 종료 대기 · Codex를 닫으면 적용됩니다.')
                # Do not drain busy or idle persistent sockets merely to force an update.
                deadline = self.clock()+86400
                while self.clock()<deadline:
                    if read_json(self.path).get('cancel_requested'):
                        self.publish('cancelled', message='업데이트 예약을 취소했습니다.'); return
                    if not m.state().get('enabled') or m.config()[1].get('openai_base_url')!=m.url:
                        self.publish('cancelled', message='프록시가 꺼져 예약을 취소했습니다.'); return
                    health = m.health(timeout=1)
                    if health and health.get('active_connections')==0:
                        break
                    self.publish('waiting'); self.sleep(2)
                else:
                    self.publish('cancelled', message='연결이 유지되어 예약을 종료했습니다. 다시 예약할 수 있습니다.'); return
                with ProcessLock(m.control_lock, timeout=10):
                    health = m.health(timeout=3)
                    if not health or health.get('instance')!=data.get('source_instance', health.get('instance')):
                        raise RuntimeError('프록시가 변경되어 업데이트를 취소했습니다.')
                    # Guard against a connection racing the zero-connection check: the
                    # existing drain protocol finishes it before the worker exits.
                    self.publish('switching', message='프록시 업데이트 중…')
                    self.drain()
                    try:
                        m.task.stop()
                        self.await_supervisor_exit()
                        m.task.start(command, autostart=True)
                        self.ready(PROXY_VERSION)
                    except Exception:
                        self.rollback(read_json(self.path)); return
                    self.publish('complete', message='프록시 업데이트 완료')
            except Exception as exc:
                if read_json(self.path).get('phase') in ('switching','rollback'):
                    self.publish('rollback',message='복구 재시도 중: '+str(exc))
                    raise
                self.publish('failed', message=str(exc))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--codex-home',required=True)
    parser.add_argument('--evidence-path',type=Path,required=True)
    parser.add_argument('--upstream')
    parser.add_argument('--port',type=int,default=8768)
    args=parser.parse_args()
    manager=ObserverManager(args.codex_home,args.evidence_path.parent,url=f'http://127.0.0.1:{args.port}')
    ProxyUpdate(manager).run()
    return 0
