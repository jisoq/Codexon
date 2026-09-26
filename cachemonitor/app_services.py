"""Application-owned service lifetime; collection remains in CollectorService."""
import json
import re
import time

from .observer_state import ProcessLock, read_json
from .observer_control import atomic_write
from .proxy_target import ProxyTarget
from . import proxy_identity as identity


def session_path(manager):
    return manager.directory/'app-services.json'


def suspended(manager):
    value=read_json(session_path(manager))
    return value.get('scope')==ProxyTarget(manager).scope and value.get('phase') in ('closing','stopped','failed','resuming')


def require_running(manager):
    if suspended(manager):raise RuntimeError('Codexon 종료 중이거나 종료된 상태입니다. 앱을 다시 열어 주세요.')


def disable_resume(manager):
    path=session_path(manager);value=read_json(path)
    if value.get('scope')==ProxyTarget(manager).scope:
        value['resume']=False;atomic_write(path,json.dumps(value).encode())


class AppServices:
    def __init__(self,manager,homes,index=None,evidence=None,*,progress=lambda text:None,
                 clock=time.monotonic,sleep=time.sleep):
        self.manager,self.homes=manager,homes
        self.index,self.evidence=index,evidence
        self.target=ProxyTarget(manager) if manager else None
        self.progress,self.clock,self.sleep=progress,clock,sleep

    def save(self,**changes):
        path=session_path(self.manager);value=read_json(path)
        if value and value.get('scope')!=self.target.scope:
            raise RuntimeError('다른 연결의 종료 기록을 보존합니다.')
        value.update(scope=self.target.scope,**changes)
        atomic_write(path,json.dumps(value,ensure_ascii=False).encode())
        return value

    def publish(self,phase,**value):
        self.progress(value.get('message','관련 프로세스 종료 확인 중…'))

    def wait(self,done,message,seconds=30):
        deadline=self.clock()+seconds
        while not done():
            if self.clock()>=deadline:raise RuntimeError(message)
            self.sleep(.2)

    def stop_proxy(self):
        from .model_evidence import home_key
        from .observer_task import ObserverTask
        from .proxy_update import ProxyUpdate, BUSY
        from .proxy_drain import ProxyDrain
        m=self.manager
        with ProcessLock(m.control_lock,timeout=30):
            previous=read_json(session_path(m))
            if previous and previous.get('scope')!=self.target.scope:
                raise RuntimeError('다른 연결의 종료 기록을 보존합니다.')
            state=m.state()
            configured=m.config()[1].get('openai_base_url')==m.url
            if previous.get('phase') not in ('closing','failed','stopped','resuming'):
                self.save(phase='closing',resume=bool(configured),state=state,source=None,
                          direct_url=state.get('previous_url') if not self.target.cache else None)
            else:self.save(phase='closing')
            updater=ProxyUpdate(m)
            if read_json(updater.path).get('phase') in BUSY:
                updater.publish(read_json(updater.path)['phase'],cancel_requested=True,
                                message='앱 종료에 따라 교체 중지 중…')
            update_task=ObserverTask(home_key(m.home),role='ProxyUpdate')
            update_task.suspend()
            for task in (m.task,m.legacy_task,m.check_task):task.suspend()
        self.progress('진행 중 설정 작업 마무리 중…')
        self.wait(lambda:identity.locks_free([m.directory/'proxy-update.lock']) and
                  not update_task.inspect().get('running'), '업데이트 프로세스 종료 확인 지연')
        with ProcessLock(m.control_lock,timeout=30):
            # A concurrently finishing updater may have re-registered its task.
            update_task.suspend()
            m.task.suspend()
            health=m.health(timeout=3)
            data=read_json(session_path(m))
            source=data.get('source')
            if not source and not health:
                interrupted=read_json(updater.path)
                if interrupted.get('scope')==self.target.scope and interrupted.get('source'):
                    source=interrupted['source']
                    self.save(source=source)
            if health:
                source=self.target.capture(health)
                self.save(source=source)
                control=source.get('control_id','')
                if not re.fullmatch('[0-9a-f]{32}',control):
                    raise RuntimeError('이 프록시는 안전 종료 제어를 지원하지 않습니다. 실행 중 응답을 유지합니다.')
                atomic_write(m.directory/('proxy-control-'+control+'.json'),
                             json.dumps(dict(id=control,action='pause')).encode())
            elif not source and not (identity.port_free(m.url) and identity.locks_free(self.target.locks)):
                raise RuntimeError('관련 프로세스 소유권을 확인하지 못해 종료를 보류했습니다.')

        def restore_route(_=None):
            with ProcessLock(m.control_lock,timeout=30):
                if m.config()[1].get('openai_base_url')==m.url:
                    direct=data.get('direct_url')
                    if direct not in (None,'https://api.openai.com/v1','https://chatgpt.com/backend-api/codex'):
                        raise RuntimeError('원래 연결 주소를 확인하지 못했습니다.')
                    m.set_url(direct)
                if not self.target.cache:
                    state=m.state();state.update(enabled=False,pending=False,phase='off')
                    m.write_state(state)

        if source:
            ProxyDrain(m,self.target,publish=self.publish,before_drain=restore_route,
                       clock=self.clock,sleep=self.sleep).run(source)
        restore_route()
        self.save(phase='stopped')

    def stop_collection(self):
        from .usage_collection import CollectionChannel, locked
        from .observer_task import ObserverTask
        channel=CollectionChannel(self.homes,self.index,self.evidence)
        try:
            with ProcessLock(channel.companion('.collector-update.lock'),timeout=30):
                from .collection_lifecycle import collector_process,retire_legacy
                retire_legacy(channel)
                snapshot=channel.read()
                collection=(snapshot or {}).get('collection') or {}
                lock=channel.companion('.collector.lock')
                process=None
                if locked(lock):
                    if not collection.get('instance'):raise RuntimeError('수집기 종료 소유권 확인 대기')
                    process=collector_process(channel,snapshot)
                atomic_write(channel.companion('.session.json'),json.dumps(dict(scope=channel.scope,stopped=True)).encode())
                task=ObserverTask(str(channel.path),role='UsageCollector');task.suspend()
                if process:
                    channel.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',(collection['instance'],'stop'))
                    channel.db.commit()
                    self.progress('수집 기록 저장과 프로세스 종료 확인 중…')
                    self.wait(lambda:identity.process_exited(process) and not locked(lock),
                              '수집 기록 저장 또는 수집기 종료 확인 지연')
                self.wait(lambda:not task.inspect().get('running'),'수집기 작업 종료 확인 지연')
        finally:channel.close()

    def stop(self):
        if self.manager:
            try:self.stop_proxy()
            except Exception:
                self.save(phase='failed');raise
        self.stop_collection()


def resume_proxy(manager):
    """Resume only the connection suspended by this app, with its saved role."""
    m=manager;target=ProxyTarget(m);path=session_path(m);data=read_json(path)
    if not data:return False
    if data.get('scope')!=target.scope:raise RuntimeError('다른 연결의 종료 기록을 보존합니다.')
    if data.get('phase')=='active':return False
    services=AppServices(m,[],progress=lambda _:None)
    if data.get('phase') in ('closing','failed','resuming'):
        services.stop_proxy();data=read_json(path)
    with ProcessLock(m.control_lock,timeout=30):
        # An explicit off or external route edit wins over saved resume intent.
        if not data.get('resume') or m.config()[1].get('openai_base_url')!=data.get('direct_url'):
            services.save(phase='active',resume=False);return True
        source=data.get('source')
        if not source:
            services.save(phase='active',resume=False);return True
        if not target.stopped(source):raise RuntimeError('이전 프로세스 종료 확인 후에만 재개할 수 있습니다.')
        command=target.replacement(source)
        services.save(phase='resuming')
        if not target.cache:
            state=dict(data['state']);state.update(enabled=False,pending=True,phase='starting')
            m.write_state(state)
        try:
            from .version import PROXY_VERSION
            import sys
            distribution=identity.deployment(command[0]) if getattr(sys,'frozen',False) else None
            m.task.start(command,autostart=False)
            deadline=time.monotonic()+30;consecutive=0;instance=None
            while time.monotonic()<deadline:
                health=m.health(timeout=1)
                valid=target.ready(health,source,command,PROXY_VERSION,distribution)
                candidate=(health or {}).get('instance')
                consecutive=consecutive+1 if valid and instance==candidate else 1 if valid else 0
                instance=candidate
                if consecutive>=3:break
                time.sleep(1)
            else:raise RuntimeError('앱 연결 재개 준비 확인 실패 · 직접 연결 유지')
            m.set_url(m.url)
            if not target.cache:
                state=dict(data['state']);state.update(enabled=True,pending=False,phase='active')
                m.write_state(state)
                m.configure_check()
            services.save(phase='active')
        except Exception:
            services.save(phase='failed');raise
    return True
