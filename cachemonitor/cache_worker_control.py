"""One connection controller for an existing cache worker and its dashboard."""
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from .observer_control import ObserverManager
from .observer_task import ObserverTask
from .observer_state import read_json,ProcessLock
from .version import VERSION,PROXY_VERSION,proxy_compatible


class CacheWorkerManager(ObserverManager):
    shared_cache_worker=True
    def __init__(self,home,index_path,evidence_path):
        self.index=Path(index_path).resolve()
        evidence=Path(evidence_path).resolve()
        super().__init__(home,evidence.parent)
        self.evidence=evidence
        self.task=ObserverTask(str(self.home),role='CacheWorker')
        self.route_path=self.index.with_name('cache-route.json')
        route=read_json(self.route_path).get('url') or self.config()[1].get('openai_base_url')
        # A cache worker is local; never adopt a remote provider as its control URL.
        parsed=urlsplit(route or '')
        if parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port and not parsed.username and parsed.path in ('','/'):
            self.url=route.rstrip('/')

    def command(self,upstream=None):
        parts=[sys.executable]
        if not getattr(sys,'frozen',False):parts.append(str(Path(__file__).resolve().parents[1]/'run.py'))
        return parts+['--model-proxy','--cache-worker','--upstream',upstream or self.upstream(),'--codex-home',str(self.home),
            '--evidence-path',str(self.evidence),'--observation-index',str(self.index),'--port',str(urlsplit(self.url).port)]

    def status(self):
        configured=self.config()[1].get('openai_base_url')==self.url
        health=self.health(timeout=3)
        if health and not health.get('cache_management'):raise RuntimeError('이 주소에서 캐시 작업기를 확인하지 못했습니다.')
        registration=self.task.inspect()
        return dict(configured=configured,enabled=configured,running=bool(health),health=health,
            phase='active' if configured and health else 'recovery_required' if configured else 'off',
            probe_state=self.health_state,runtime={'phase':'active' if health else 'stopped'},registration=registration,
            app_version=VERSION,app_path=sys.executable,version_mismatch=bool(health and not proxy_compatible(health.get('version'))),
            target_proxy_version=PROXY_VERSION,proxy_update_available=bool(health and health.get('version')!=PROXY_VERSION),
            update=self.update_status(),
            restart_required=bool(not configured and self.state().get('restart_required')),
            running_proxy_path=self.running_path(health),
            shared_cache_worker=True,url=self.url,evidence_path=str(self.evidence))

    def ensure(self):return self.status()

    def resume(self):
        from .app_services import resume_proxy
        from .proxy_update import BUSY
        if resume_proxy(self):return self.status()
        with ProcessLock(self.control_lock,timeout=30):
            if read_json(self.directory/'proxy-update.json').get('phase') in BUSY:return self.status()
            from .proxy_target import ProxyTarget
            target=ProxyTarget(self)
            health=self.health(timeout=3)
            if health:
                source=target.capture(health)
                self.task.configure(source['command'],autostart=False)
            elif target.enabled() and self.health_state=='refused':
                self.restart_registered(target)
        return self.status()

    def restart_registered(self,target):
        """Recover a rebooted worker on GUI launch, retaining its registered role."""
        from .launch_context import command_arguments
        from . import proxy_identity as identity
        registration=self.task.inspect()
        if registration.get('running'):return
        if not registration.get('registered') or not registration.get('executable'):
            raise RuntimeError('캐시 작업기 실행 정보가 없습니다. 프록시를 다시 켜 주세요.')
        command=[registration['executable'],*command_arguments('worker '+registration.get('arguments',''))[1:]]
        target.validate_command(command)
        # Preserve the role/paths, but use this verified app and discard old
        # drain controls. A cold start must not depend on an obsolete payload.
        command=target.replacement({'command':command})
        if not identity.port_free(self.url) or not identity.locks_free(target.locks):
            raise RuntimeError('기존 캐시 작업기 종료를 확인하지 못했습니다. 연결을 유지합니다.')
        self.task.start(command,autostart=False)
        deadline=time.monotonic()+30
        while not self.cancelled.is_set() and time.monotonic()<deadline:
            health=self.health(timeout=1)
            if health:
                source=target.capture(health)
                if (not health.get('cache_management') or health.get('draining') or
                        Path(source['command'][0]).resolve()!=Path(command[0]).resolve() or
                        ('--cache-observe-only' in source['command'])!=('--cache-observe-only' in command)):
                    raise RuntimeError('등록된 캐시 작업기와 실행 중 연결이 다릅니다.')
                return
            time.sleep(.2)
        raise RuntimeError('캐시 작업기 시작을 확인하지 못했습니다. 연결 설정은 유지됩니다.')

    def test_connection(self):
        # An extra model probe would bypass the cache execution grant and ledger.
        return self.status()

    def turn_on(self):
        with ProcessLock(self.control_lock,timeout=5):
            from .app_services import require_running
            require_running(self)
            configured=self.config()[1].get('openai_base_url')
            if configured not in (None,self.url):raise RuntimeError('다른 연결이 설정되어 있습니다. 기존 연결을 보존합니다.')
            health=self.health(timeout=3)
            if health and not health.get('cache_management'):raise RuntimeError('캐시 작업기가 아닌 연결을 보존합니다.')
            if not health:
                if self.health_state!='refused':raise RuntimeError('기존 연결 상태를 확인하지 못했습니다.')
                self.task.start(self.command(),autostart=False)
                until=time.monotonic()+15
                while not self.cancelled.is_set() and time.monotonic()<until:
                    health=self.health(timeout=1)
                    if health:break
                    time.sleep(.2)
                if not health or self.cancelled.is_set():raise RuntimeError('연결 준비가 완료되지 않았습니다. 주소는 변경하지 않았습니다.')
            self.task.configure(self.command(),autostart=False)
            from .observer_control import atomic_write
            import json
            atomic_write(self.route_path,json.dumps({'url':self.url}).encode())
            self.set_url(self.url)
        return self.status()

    def turn_off(self):
        with ProcessLock(self.control_lock,timeout=5):
            from .app_services import disable_resume
            disable_resume(self)
            if self.config()[1].get('openai_base_url') not in (None,self.url):raise RuntimeError('다른 연결을 보존합니다.')
            from .observer_control import atomic_write
            import json
            atomic_write(self.route_path,json.dumps({'url':self.url}).encode())
            from .cache_db import revoke_grants
            revoke_grants(self.index.with_name('cache-control.sqlite'),self.home)
            self.set_url(None);self.task.configure(self.command(),autostart=False)
        # Never close a user's already established stream.
        return {**self.status(),'restart_required':True}

    def update_proxy(self):
        return super().update_proxy()

    def recover_direct(self):
        # Recovery/uninstall removes registration and drains without killing a
        # live user stream. Scheduler cleanup still collects sent usage.
        import json,re
        from .observer_control import atomic_write
        from .cache_db import revoke_grants
        revoke_grants(self.index.with_name('cache-control.sqlite'),self.home)
        health=self.health(timeout=3)
        result=super().recover_direct()
        identity=(health or {}).get('control_id','')
        if health and health.get('cache_management') and re.fullmatch(r'[0-9a-f]{32}',identity):
            atomic_write(self.directory/('proxy-control-'+identity+'.json'),json.dumps(dict(action='drain',id=identity)).encode())
        return result
