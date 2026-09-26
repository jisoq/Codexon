"""Application-owned service lifetime; collection remains in CollectorService."""
import json
import re
import time
import sys
import threading
from pathlib import Path

from .observer_state import ProcessLock, read_json
from .observer_control import atomic_write
from .proxy_target import ProxyTarget, option
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
                 clock=time.monotonic,sleep=time.sleep,collection=True):
        self.manager,self.homes=manager,homes
        self.index,self.evidence=index,evidence
        self.target=ProxyTarget(manager) if manager else None
        self.progress,self.clock,self.sleep=progress,clock,sleep
        self.collection=collection
        self.active=False
        self.closing=threading.Event()
        self.operation_lock=threading.RLock()
        self.retries={}
        self.proxy_source=None
        self.collector_source=None

    def deactivate(self):
        self.closing.set()
        self.active=False

    def execute(self,operation):
        """Serialize UI actions and the app's single periodic operation."""
        with self.operation_lock:
            if self.closing.is_set():return {}
            if operation in ('start','ensure'):
                result=self.start() if operation=='start' else self.poll()
                if not result.get('collection_issue') and not result.get('error'):
                    from .install_cleanup import after_services
                    try:after_services(self)
                    except (OSError,ValueError,RuntimeError) as exc:
                        result['cleanup_warning']=str(exc)
                return result
            if operation in ('turn_on','resume'):
                self.retries.pop('proxy',None)
            result=getattr(self.manager,operation)()
            self.remember_proxy(result)
            return result

    def status(self):
        return self.manager.status() if self.manager else {}

    def remember_proxy(self,result):
        health=result.get('health') or {}
        if health.get('instance') and health['instance']!=(self.proxy_source or {}).get('instance'):
            self.proxy_source=self.target.capture(health)

    def start(self):
        self.active=True
        errors=[]
        if self.collection:
            try:self.start_collection()
            except (OSError,ValueError,RuntimeError) as exc:errors.append(str(exc))
        result={}
        if self.manager and not self.closing.is_set():
            self.manager.cleanup_legacy_check()
            self.manager.adopt_registrations()
            if not self.closing.is_set():
                result=self.manager.resume()
                self.remember_proxy(result)
        if errors:result['collection_issue']='\n'.join(errors)
        return result

    def collector_identity(self,channel,snapshot):
        source=(snapshot or {}).get('collection') or {}
        if not source.get('pid'):return None
        process=identity.process_identity(source['pid'])
        if not process:return None
        saved=self.collector_source
        if saved and saved['process']==process and saved['instance']==source.get('instance'):return saved
        command=identity.process_command(process['pid'])
        homes=[str(Path(command[n+1]).resolve()) for n,arg in enumerate(command[:-1]) if arg=='--codex-home']
        if ('--usage-collector' not in command or not homes or not set(homes).issubset(set(channel.homes)) or
                Path(option(command,'--index-path','')).resolve()!=channel.path or
                Path(option(command,'--evidence-path',str(channel.default_evidence))).resolve()!=Path(channel.scope) or
                Path(process['executable']).resolve()!=Path(source.get('executable','')).resolve()):
            raise RuntimeError('다른 수집기의 실행 정보를 보존합니다.')
        self.collector_source=dict(process=process,instance=source['instance'])
        return self.collector_source

    def start_collection(self):
        """Startup/adoption is the only collection migration entry point."""
        from .usage_collection import CollectionChannel,collector_command,locked
        from .collection_lifecycle import retire_legacy
        from .observer_task import ObserverTask
        from .version import VERSION
        channel=CollectionChannel(self.homes,self.index,self.evidence)
        try:
            with ProcessLock(channel.companion('.collector-update.lock'),timeout=30):
                task=ObserverTask(str(channel.path),role='UsageCollector')
                command=collector_command(channel)
                retire_legacy(channel)
                snapshot=channel.read()
                source=self.collector_identity(channel,snapshot)
                version=((snapshot or {}).get('collection') or {}).get('version','')
                newer=tuple(int(p) for p in version.split('.') if p.isdigit())>tuple(int(p) for p in VERSION.split('.') if p.isdigit())
                if newer and source:raise RuntimeError('새 버전 수집기를 이전 앱으로 교체하지 않습니다.')
                replace=source and (version!=VERSION or
                    getattr(sys,'frozen',False) and Path(source['process']['executable']).resolve()!=Path(sys.executable).resolve())
                if replace:
                    task.configure(command,autostart=False)
                    channel.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',(source['instance'],'stop'))
                    channel.db.commit()
                    self.wait(lambda:not identity.same_process(source['process']) and not locked(channel.companion('.collector.lock')),
                              '수집 기록 저장과 이전 수집기 종료 대기')
                    source=None
                if self.closing.is_set():return
                atomic_write(channel.companion('.session.json'),json.dumps(dict(scope=channel.scope,stopped=False)).encode())
                if source:
                    task.configure(command,autostart=False)
                elif not locked(channel.companion('.collector.lock')) and not task.inspect().get('running'):
                    task.start(command,autostart=False)
        finally:channel.close()

    def retry(self,name,dead,start):
        """A successful restart never replenishes this app session's budget."""
        state=self.retries.setdefault(name,dict(at=None,count=0))
        if not dead:
            state['at']=None
            return
        now=self.clock()
        if state['at'] is None:state['at']=now+60
        if now<state['at'] or state['count']>=3 or self.closing.is_set():return
        state['count']+=1;state['at']=now+60
        start()

    def poll_collection(self):
        from .usage_collection import CollectionChannel,collector_command,locked
        from .observer_task import ObserverTask
        channel=CollectionChannel(self.homes,self.index,self.evidence)
        try:
            with ProcessLock(channel.companion('.collector-update.lock')):
                snapshot=channel.read()
                source=self.collector_identity(channel,snapshot)
                stopped=read_json(channel.companion('.session.json'))
                task=ObserverTask(str(channel.path),role='UsageCollector')
                dead=(not source and not locked(channel.companion('.collector.lock')) and
                      not task.inspect().get('running') and not (stopped.get('scope')==channel.scope and stopped.get('stopped')))
                self.retry('collection',dead,lambda:task.start(collector_command(channel),autostart=False))
                return '수집 결과 갱신 지연' if not snapshot or time.time()-snapshot['ts']>30 else ''
        finally:channel.close()

    def poll(self):
        if not self.active or self.closing.is_set():return {}
        from .proxy_update import BUSY
        result=self.status()
        if self.collection:
            try:result['collection_issue']=self.poll_collection()
            except (OSError,ValueError,RuntimeError) as exc:result['collection_issue']=str(exc)
        if self.manager:
            self.remember_proxy(result)
            m=self.manager
            with ProcessLock(m.control_lock):
                updating=read_json(m.directory/'proxy-update.json').get('phase') in BUSY
                dead=(not updating and self.target.enabled() and result.get('probe_state')=='refused' and
                      not m.task.inspect().get('running') and identity.port_free(m.url) and identity.locks_free(self.target.locks) and
                      (not self.proxy_source or self.target.exited(self.proxy_source)))
            # Role-specific resume validates ownership again under its control lock.
            self.retry('proxy',dead,m.resume)
        return result

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
        m.cleanup_legacy_check()
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
            for task in (m.task,m.legacy_task):task.suspend()
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
                from .collection_lifecycle import retire_legacy
                retire_legacy(channel)
                snapshot=channel.read()
                collection=(snapshot or {}).get('collection') or {}
                lock=channel.companion('.collector.lock')
                process=None
                if locked(lock):
                    if not collection.get('instance'):raise RuntimeError('수집기 종료 소유권 확인 대기')
                    process=identity.process_identity(collection['pid'])
                    if not process:raise RuntimeError('수집기 실행 정보가 변경되었습니다.')
                    command=identity.process_command(process['pid'])
                    if ('--usage-collector' not in command or
                        Path(option(command,'--index-path','')).resolve()!=channel.path or
                        Path(process['executable']).resolve()!=Path(collection['executable']).resolve() or
                        not set((snapshot or {}).get('homes',[])).issubset(set(channel.homes))):
                        raise RuntimeError('다른 수집기 또는 다른 Codex 홈의 기록을 보존합니다.')
                atomic_write(channel.companion('.session.json'),json.dumps(dict(scope=channel.scope,stopped=True)).encode())
                task=ObserverTask(str(channel.path),role='UsageCollector');task.suspend()
                if process:
                    channel.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',(collection['instance'],'stop'))
                    channel.db.commit()
                    self.progress('수집 기록 저장과 프로세스 종료 확인 중…')
                    self.wait(lambda:not identity.same_process(process) and not locked(lock),
                              '수집 기록 저장 또는 수집기 종료 확인 지연')
                self.wait(lambda:not task.inspect().get('running'),'수집기 작업 종료 확인 지연')
        finally:channel.close()

    def stop(self):
        self.deactivate()
        if self.manager:
            try:self.stop_proxy()
            except Exception:
                self.save(phase='failed');raise
        if self.collection:self.stop_collection()


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
            services.save(phase='active')
        except Exception:
            services.save(phase='failed');raise
    return True
