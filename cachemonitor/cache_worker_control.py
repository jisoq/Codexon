"""One connection controller for an existing cache worker and its dashboard."""
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from .observer_control import ObserverManager
from .observer_task import ObserverTask
from .observer_state import read_json,ProcessLock
from .version import VERSION,proxy_compatible


class CacheWorkerManager(ObserverManager):
    shared_cache_worker=True
    def __init__(self,home,index_path,evidence_path):
        self.index=Path(index_path)
        super().__init__(home,Path(evidence_path).parent)
        self.evidence=Path(evidence_path)
        self.task=ObserverTask(str(self.home),role='CacheWorker')
        self.route_path=self.index.with_name('cache-route.json')
        route=read_json(self.route_path).get('url') or self.config()[1].get('openai_base_url')
        # A cache worker is local; never adopt a remote provider as its control URL.
        parsed=urlsplit(route or '')
        if parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port and not parsed.username and parsed.path in ('','/'):
            self.url=route.rstrip('/')

    def command(self,upstream='chatgpt'):
        parts=[sys.executable]
        if not getattr(sys,'frozen',False):parts.append(str(Path(__file__).resolve().parents[1]/'run.py'))
        return parts+['--model-proxy','--cache-worker','--codex-home',str(self.home),
            '--evidence-path',str(self.evidence),'--observation-index',str(self.index),'--port',str(urlsplit(self.url).port)]

    def status(self):
        configured=self.config()[1].get('openai_base_url')==self.url
        health=self.health(timeout=3)
        if health and not health.get('cache_management'):raise RuntimeError('이 주소에서 캐시 작업기를 확인하지 못했습니다.')
        registration=self.task.inspect()
        return dict(configured=configured,enabled=configured,running=bool(health),health=health,
            phase='active' if configured and health else 'recovery_required' if configured else 'off',
            probe_state=self.health_state,runtime={'phase':'active' if health else 'stopped'},registration=registration,
            app_version=VERSION,app_path=self.command()[0],version_mismatch=bool(health and not proxy_compatible(health.get('version'))),
            shared_cache_worker=True,url=self.url,evidence_path=str(self.evidence))

    def ensure(self):return self.status()

    def test_connection(self):
        # An extra model probe would bypass the cache execution grant and ledger.
        return self.status()

    def turn_on(self):
        with ProcessLock(self.control_lock,timeout=5):
            configured=self.config()[1].get('openai_base_url')
            if configured not in (None,self.url):raise RuntimeError('다른 연결이 설정되어 있습니다. 기존 연결을 보존합니다.')
            health=self.health(timeout=3)
            if health and not health.get('cache_management'):raise RuntimeError('캐시 작업기가 아닌 연결을 보존합니다.')
            if not health:
                if self.health_state!='refused':raise RuntimeError('기존 연결 상태를 확인하지 못했습니다.')
                self.task.start(self.command(),autostart=True)
                until=time.monotonic()+15
                while not self.cancelled.is_set() and time.monotonic()<until:
                    health=self.health(timeout=1)
                    if health:break
                    time.sleep(.2)
                if not health or self.cancelled.is_set():raise RuntimeError('연결 준비가 완료되지 않았습니다. 주소는 변경하지 않았습니다.')
            self.task.configure(self.command(),autostart=True)
            from .observer_control import atomic_write
            import json
            atomic_write(self.route_path,json.dumps({'url':self.url}).encode())
            self.set_url(self.url)
        return self.status()

    def turn_off(self):
        with ProcessLock(self.control_lock,timeout=5):
            if self.config()[1].get('openai_base_url') not in (None,self.url):raise RuntimeError('다른 연결을 보존합니다.')
            from .observer_control import atomic_write
            import json
            atomic_write(self.route_path,json.dumps({'url':self.url}).encode())
            from .cache_execution import Journal
            journal=Journal(self.index.with_name('cache-control.sqlite'))
            try:
                for grant in journal.operations.grants(str(self.home)):journal.operations.stop(grant['id'],'revoked')
            finally:journal.close()
            self.set_url(None);self.task.configure(self.command(),autostart=False)
        # Never close a user's already established stream.
        return {**self.status(),'restart_required':True}

    def update_proxy(self):
        self.task.configure(self.command(),autostart=self.config()[1].get('openai_base_url')==self.url)
        return {**self.status(),'update':{'phase':'waiting','message':'기존 연결 유지 중 · 다음 작업기 기동부터 설치본 적용'}}
