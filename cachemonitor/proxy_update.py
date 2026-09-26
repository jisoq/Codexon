"""Independent, same-address proxy replacement with persistent rollback metadata."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from .observer_control import ObserverManager, atomic_write
from .observer_state import ProcessLock, read_json
from .version import VERSION, PROXY_VERSION

BUSY = ('queued', 'waiting', 'switching', 'stopping', 'starting', 'verifying', 'rollback')


def process_executable(pid):
    from .proxy_identity import process_identity
    value=process_identity(pid)
    if not value:raise OSError('Proxy process has exited')
    return value['executable']


class UpdateCancelled(Exception):
    pass


class ProxyUpdate:
    def __init__(self, manager, clock=time.monotonic, sleep=time.sleep, target=None):
        from .proxy_target import ProxyTarget
        self.manager, self.clock, self.sleep = manager, clock, sleep
        self.target=target or ProxyTarget(manager)
        self.path = manager.directory/'proxy-update.json'

    def publish(self, phase, **extra):
        with ProcessLock(self.manager.directory/'proxy-update-journal.lock',timeout=5):
            data = read_json(self.path)
            data.update(phase=phase, updated_at=time.time(), target_version=PROXY_VERSION, **extra)
            atomic_write(self.path, json.dumps(data, ensure_ascii=False).encode())

    def allowed(self):
        if read_json(self.path).get('cancel_requested') or not self.target.enabled():
            raise UpdateCancelled()

    def preflight(self):
        from .proxy_identity import deployment
        distribution=deployment(sys.executable)
        report=self.manager.directory/'proxy-update-runtime.json'
        report.unlink(missing_ok=True)
        result=subprocess.run([sys.executable,'--verify-runtime',str(report)],timeout=30,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        value=read_json(report)
        if (result.returncode or value.get('version')!=VERSION
                or value.get('proxy_version')!=PROXY_VERSION or value.get('errors')!=[]):
            raise RuntimeError('새 배포본 실행 검사 실패 · 기존 프록시를 유지합니다.')
        return distribution

    def ready(self, source, command, version, distribution=None):
        deadline=self.clock()+30;consecutive=0;last_instance=None
        while self.clock()<deadline:
            self.allowed()
            health=self.manager.health(timeout=1)
            valid=self.target.ready(health,source,command,version,distribution)
            instance=(health or {}).get('instance')
            consecutive=consecutive+1 if valid and instance==last_instance else 1 if valid else 0
            last_instance=instance
            if consecutive>=3:return health
            self.sleep(1)
        raise RuntimeError('실제 버전·역할·경로·준비 완료 확인 실패')

    def drain(self, source):
        from .proxy_drain import ProxyDrain
        return ProxyDrain(self.manager,self.target,allowed=self.allowed,publish=self.publish,
                          clock=self.clock,sleep=self.sleep).run(source)

    def start(self,command,source):
        self.allowed()
        if not self.target.stopped(source):raise RuntimeError('기존 프로세스·포트·실행 잠금이 남아 있습니다.')
        # Recheck the user's off/cancel action while holding the same control lock.
        with ProcessLock(self.manager.control_lock,timeout=5):
            self.allowed()
            if not self.target.cache and source.get('state_phase'):
                state=self.manager.state()
                if state.get('replacement_instance')==source['instance']:
                    state['phase']=source['state_phase'];state.pop('replacement_instance',None)
                    self.manager.write_state(state)
            if '--control-file' in command:
                from .proxy_target import option
                atomic_write(Path(option(command,'--control-file')),json.dumps(
                    dict(action='run',id=option(command,'--control-id'))).encode())
            self.manager.task.start(command,autostart=source['registration'].get('autostart',False))

    def rollback(self,data):
        self.allowed()
        source=data['source'];m=self.manager
        if source.get('executable_sha256'):
            from .proxy_identity import digest
            if digest(source['command'][0])!=source['executable_sha256']:
                raise RuntimeError('복구 실행 파일 무결성 확인 실패')
        self.publish('rollback',message='이전 역할과 설정으로 복구 중')
        current=m.health(timeout=3)
        if current:
            if current['instance']!=source['instance']:
                try:
                    if self.target.ready(current,source,source['command'],source['version']):
                        self.ready(source,source['command'],source['version'])
                        self.publish('failed',message='이전 버전 복구 확인 완료',restored=True);return
                except (OSError,RuntimeError):pass
            # Only drain the newly launched owned deployment, never a port rival.
            owned=self.target.capture(current)
            if Path(owned['command'][0]).resolve()!=Path(data['command'][0]).resolve():
                raise RuntimeError('복구 대상과 다른 프로세스를 보존합니다.')
            self.drain(owned)
            # All new processes must exit before restoring the original command.
            if not self.target.stopped(owned):raise RuntimeError('새 프로세스 종료 확인 실패')
        self.publish('rollback',message='이전 실행 명령으로 재기동 중')
        self.start(source['command'],source)
        self.ready(source,source['command'],source['version'])
        self.publish('failed',message='업데이트 실패 · 이전 버전 복구 확인 완료',restored=True)

    def run(self):
        try:return self.replace()
        finally:
            # A repetition trigger also recovers on-demand updater crashes on
            # Windows systems that do not apply RestartOnFailure to that launch.
            # Disable it after a terminal result, under the same UI transaction
            # lock so a new authorized update cannot lose its recovery trigger.
            if read_json(self.path).get('phase') in ('complete','cancelled','failed'):
                from .observer_task import ObserverTask
                from .model_evidence import home_key
                if hasattr(self.manager,'home'):
                    with ProcessLock(self.manager.control_lock,timeout=5):
                        if read_json(self.path).get('phase') in ('complete','cancelled','failed'):
                            ObserverTask(home_key(self.manager.home),role='ProxyUpdate').finish_update()

    def replace(self):
        m=self.manager
        try:
            with ProcessLock(m.directory/'proxy-update.lock'):
                data=read_json(self.path)
                if data.get('phase') in ('complete','cancelled'):return
                try:
                    self.allowed()
                    if data.get('scope') and data['scope']!=self.target.scope:
                        raise RuntimeError('다른 연결의 업데이트 기록을 보존합니다.')
                    distribution=self.preflight()
                    self.allowed()
                    source=data.get('source')
                    command=data.get('command')
                    if not source:
                        health=m.health(timeout=3)
                        if not health:raise RuntimeError('기존 프록시를 확인하지 못했습니다.')
                        if health['instance']!=data.get('source_instance',health['instance']):
                            raise RuntimeError('교체 요청 뒤 프록시 인스턴스가 바뀌었습니다.')
                        source=self.target.capture(health)
                        command=self.target.replacement(source)
                        # Even a same-version installation must prove its policy
                        # and actual deployment before skipping replacement.
                        probe={**source,'instance':''}
                        if self.target.ready(health,probe,command,PROXY_VERSION,distribution):
                            self.ready(probe,command,PROXY_VERSION,distribution)
                            self.publish('complete',message='목표 프록시 실행 확인 완료');return
                        if not source.get('control_id'):
                            raise RuntimeError('실행 중인 구버전에 안전 종료 제어가 없습니다. 기존 응답을 보존했습니다.')
                        self.publish('waiting',source=source,command=command,distribution=distribution,
                                     scope=self.target.scope,message='연결 교체 준비 중')
                    data=read_json(self.path)
                    if data.get('phase')=='rollback':
                        self.rollback(data);return
                    current=m.health(timeout=3)
                    if current and current['instance']!=source['instance']:
                        # A restarted updater adopts a verified successful launch.
                        try:
                            self.ready(source,command,PROXY_VERSION,distribution)
                            self.publish('complete',message='프록시 업데이트 완료');return
                        except UpdateCancelled:raise
                        except Exception:
                            self.rollback(data);return
                    if current:self.drain(source)
                    elif not self.target.stopped(source):self.drain(source)
                    self.allowed()
                    self.publish('starting',message='새 프록시 기동 중')
                    try:
                        self.start(command,source)
                        self.publish('verifying',message='새 버전·역할·실행 경로 확인 중')
                        ready=self.ready(source,command,PROXY_VERSION,distribution)
                        self.publish('complete',message='프록시 업데이트 완료',result=dict(
                            version=ready['version'],instance=ready['instance'],pid=ready['pid'],
                            executable=ready.get('executable'),role=ready.get('role')))
                    except UpdateCancelled:raise
                    except Exception as exc:
                        self.publish('rollback',failure=str(exc))
                        self.rollback(read_json(self.path))
                except UpdateCancelled:
                    self.publish('cancelled',message='연결 비활성화 또는 취소로 재기동을 중단했습니다.')
                except Exception as exc:
                    # Never stop a process merely because verification failed.
                    # Scheduler can resume this journal without duplicating a send.
                    saved=read_json(self.path)
                    if saved.get('source') and saved.get('phase')!='rollback':
                        try:
                            if self.target.stopped(saved['source']):
                                self.rollback(saved);return
                        except UpdateCancelled:
                            self.publish('cancelled',message='비활성화된 연결의 복구를 취소했습니다.');return
                        except Exception as recovery:
                            self.publish('rollback',message='복구 재시도 필요: '+str(recovery));raise
                    phase='rollback' if read_json(self.path).get('phase')=='rollback' else 'failed'
                    self.publish(phase,message=str(exc))
                    if phase=='rollback':raise
        except RuntimeError as exc:
            if '다른 프록시 설정 작업' in str(exc):return
            raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--codex-home',required=True)
    parser.add_argument('--evidence-path',type=Path,required=True)
    parser.add_argument('--upstream')
    parser.add_argument('--port',type=int,default=8768)
    parser.add_argument('--cache-worker',action='store_true')
    parser.add_argument('--cache-observe-only',action='store_true')
    parser.add_argument('--observation-index',type=Path)
    args=parser.parse_args()
    if args.cache_worker or args.cache_observe_only:
        if not args.observation_index:parser.error('Cache worker index required')
        from .cache_worker_control import CacheWorkerManager
        manager=CacheWorkerManager(args.codex_home,args.observation_index,args.evidence_path)
        if manager.url!=f'http://127.0.0.1:{args.port}':raise RuntimeError('캐시 경로 주소와 교체 주소가 다릅니다.')
    else:manager=ObserverManager(args.codex_home,args.evidence_path.parent,url=f'http://127.0.0.1:{args.port}')
    ProxyUpdate(manager).run()
    return 0
