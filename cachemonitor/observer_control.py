"""Opt-in packaged observer lifecycle and reversible Codex configuration."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
import tempfile
import sqlite3
import threading
import errno

import tomlkit

from .model_evidence import default_path, home_key
from .observer_task import ObserverTask
from .version import VERSION, PROXY_VERSION, proxy_compatible
from .observer_state import ProcessLock, read_json

URL = 'http://127.0.0.1:8768'
STARTUP_NAME = 'CacheMonitorModelProxy'


class LocalHealthOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def startup_value(value=...):
    if os.name != 'nt':
        return None
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        if value is ...:
            try:return winreg.QueryValueEx(key, STARTUP_NAME)[0]
            except FileNotFoundError:return None
        if value is None:
            try:winreg.DeleteValue(key,STARTUP_NAME)
            except FileNotFoundError:pass
        else:winreg.SetValueEx(key,STARTUP_NAME,0,winreg.REG_SZ,value)


def atomic_write(path, data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_bytes(data)
        for attempt in range(6):
            try:
                os.replace(temporary,path)
                break
            except OSError as error:
                if getattr(error,'winerror',None) not in (5,32,33) or attempt==5:
                    raise
                time.sleep(.02*(attempt+1))
    finally:
        temporary.unlink(missing_ok=True)


class ObserverManager:
    def __init__(self, home, directory=None, url=URL):
        self.home=Path(home).resolve()
        self.directory=Path(directory) if directory else default_path().parent
        self.evidence=self.directory/'model-evidence.sqlite'
        self.state_path=self.directory/'model-observer.json'
        self.config_path=self.home/'config.toml'
        self.url=url.rstrip('/')
        self.task=ObserverTask(home_key(self.home), role='ProxySupervisor')
        self.legacy_task=ObserverTask(home_key(self.home))
        self.runtime_path=self.directory/'proxy-runtime.json'
        self.control_lock=self.directory/'observer-control.lock'
        self.cancelled=threading.Event()
        if url==URL:
            # Reopen the app-owned address, including a nondefault local port.
            # Health still verifies the home/evidence identity before control.
            saved=read_json(self.state_path)
            if saved.get('home')==home_key(self.home) and saved.get('url'):
                from urllib.parse import urlsplit
                address=urlsplit(saved['url'])
                if address.scheme=='http' and address.hostname=='127.0.0.1' and address.port and not address.username and address.path in ('','/'):
                    self.url=saved['url'].rstrip('/')

    def state(self):
        if not self.state_path.exists():return {}
        state=json.loads(self.state_path.read_text(encoding='utf-8-sig'))
        if not isinstance(state,dict) or state.get('home')!=home_key(self.home):
            raise ValueError('다른 Codex 홈의 모델 검증 설정입니다')
        return state

    def write_state(self,state):
        atomic_write(self.state_path,json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))

    def config(self):
        raw=self.config_path.read_bytes() if self.config_path.exists() else b''
        return raw,tomllib.loads(raw.decode('utf-8-sig'))

    def set_url(self,value):
        raw,_=self.config()
        document=tomlkit.parse(raw.decode('utf-8-sig'))
        if value is None:
            document.pop('openai_base_url',None)
        else:document['openai_base_url']=value
        updated=tomlkit.dumps(document).encode('utf-8')
        if raw.startswith(b'\xef\xbb\xbf'):updated=b'\xef\xbb\xbf'+updated
        if raw==updated:return None
        backup=self.directory/'backups'/('codex-config-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.toml')
        atomic_write(backup,raw)
        if (self.config_path.read_bytes() if self.config_path.exists() else b'')!=raw:
            raise RuntimeError('Codex 설정이 다른 곳에서 변경됐습니다. 다시 시도하세요.')
        atomic_write(self.config_path,updated)
        return str(backup)

    def health(self,timeout=.4):
        self.health_state='unknown'
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),LocalHealthOnly())
            with opener.open(self.url+'/health',timeout=timeout) as response:
                raw=response.read(262145)
                if len(raw)>262144:return None
                result=json.loads(raw)
        except (OSError,ValueError,urllib.error.URLError) as exc:
            reason=getattr(exc,'reason',exc)
            if isinstance(reason,ConnectionRefusedError) or (isinstance(reason,OSError) and
                    (getattr(reason,'errno',None) in (errno.ECONNREFUSED,10061)
                     or getattr(reason,'winerror',None)==10061)):
                self.health_state='refused'
            return None
        if (not isinstance(result,dict) or not isinstance(result.get('service'),str)
                or not result['service'] or not isinstance(result.get('identity'),str)
                or not result['identity']):
            return None
        expected=hashlib.sha256((home_key(self.home)+'|'+str(self.evidence.resolve())).encode()).hexdigest()
        if result.get('service')!='cachemonitor-model-observer' or result.get('identity')!=expected:
            self.health_state='identity_mismatch'
            raise RuntimeError('모델 검증 포트를 다른 프로그램이나 다른 기록 경로가 사용하고 있습니다')
        if result.get('status') not in ('ok','degraded') or not isinstance(result.get('instance'),str) or not result['instance']:
            return None
        self.health_state='healthy'
        return result

    def command(self,upstream):
        if getattr(sys,'frozen',False):command=[sys.executable,'--model-proxy']
        else:
            executable=Path(sys.executable).with_name('pythonw.exe') if os.name=='nt' else Path(sys.executable)
            command=[str(executable),str(Path(__file__).resolve().parents[1]/'run.py'),'--model-proxy']
        command += ['--codex-home',str(self.home),'--evidence-path',str(self.evidence),'--upstream',upstream]
        from urllib.parse import urlsplit
        port=urlsplit(self.url).port
        if port != 8768: command += ['--port',str(port)]
        return command

    def supervisor_command(self,upstream):
        # Keep the historical task name, but run the relay itself, without a parent watcher.
        return self.command(upstream)+['--managed']

    def cleanup_legacy_check(self):
        """Retire only the old checker for this exact home, data directory and URL."""
        from .launch_context import command_arguments
        from .proxy_target import option
        task=ObserverTask(home_key(self.home),role='ConnectionCheck')
        registration=task.inspect()
        if not registration.get('registered'):return
        command=command_arguments('checker '+registration.get('arguments',''))
        if ('--check' not in command or
                Path(option(command,'--codex-home','')).resolve()!=self.home.resolve() or
                Path(option(command,'--data-dir','')).resolve()!=self.directory.resolve() or
                option(command,'--proxy-url')!=self.url):return
        task.suspend()
        task.stop()
        deadline=time.monotonic()+10
        while task.inspect().get('running'):
            if time.monotonic()>=deadline:raise RuntimeError('이전 연결 점검 종료 대기')
            time.sleep(.1)
        task.remove()
        for name in ('connection-check.json','connection-check.lock'):
            (self.directory/name).unlink(missing_ok=True)

    def adopt_registrations(self):
        """Remove autonomous triggers only from registrations for this connection."""
        from .launch_context import command_arguments
        from .proxy_target import ProxyTarget
        target=ProxyTarget(self)
        for task in (self.task,self.legacy_task):
            registration=task.inspect()
            if not registration.get('registered') or not registration.get('executable'):continue
            command=[registration['executable'],*command_arguments('worker '+registration.get('arguments',''))[1:]]
            try:target.validate_command(command)
            except (ValueError,RuntimeError):continue
            task.configure(command,autostart=False)

    def runtime(self):
        value=read_json(self.runtime_path)
        if value.get('home') != home_key(self.home) or value.get('url') != self.url: return {}
        if time.time()-value.get('updated_at',0)>10:return {}
        return value

    def start(self,upstream):
        health=self.health()
        if health:
            if not proxy_compatible(health.get('version')):
                raise RuntimeError('이전 프록시가 연결을 유지하고 있습니다. 다음 Windows 로그인 후 새 버전에서 다시 켜세요.')
        self.task.start(self.supervisor_command(upstream),autostart=False)
        for _ in range(60):
            if self.cancelled.is_set():raise RuntimeError('연결 시험을 취소했습니다. 직접 연결은 유지됩니다.')
            health=self.health()
            if health and health.get('version')==PROXY_VERSION and self.runtime().get('phase')=='ready':return
            time.sleep(.1)
        raise RuntimeError('모델 검증 서비스를 시작하지 못했습니다. Codex 연결 설정은 적용하지 않았습니다.')

    def proof_valid(self,state,health):
        return bool(isinstance(state.get('proof_at'),(int,float)) and health and health.get('instance') and state.get('proof_instance')==health['instance']
                    and proxy_compatible(health.get('version')) and health.get('status')=='ok'
                    and 0<=time.time()-state.get('proof_at',0)<300)

    def prepare(self):
        _,config=self.config()
        if config.get('openai_base_url')==self.url:
            raise RuntimeError('이미 프록시가 적용돼 있습니다. 설정에서 끈 뒤 다시 켜세요.')
        upstream=self.upstream()
        # Never overwrite a corrupt recovery record without saving it first.
        try:state=self.state()
        except (ValueError,OSError):
            if self.state_path.exists():
                atomic_write(self.directory/'backups'/('observer-state-'+uuid.uuid4().hex+'.json'),self.state_path.read_bytes())
            state={}
        before=self.state_path.read_bytes() if self.state_path.exists() else None
        state.update(home=home_key(self.home),enabled=False,pending=True,phase='starting',
                     upstream=upstream,proof_at=0,proof_instance=None,last_error=None,incident=None)
        self.write_state(state)
        try:self.start(upstream)
        except Exception:
            if before is None:self.state_path.unlink(missing_ok=True)
            else:atomic_write(self.state_path,before)
            raise
        state.update(pending=False,phase='prepared')
        self.write_state(state)
        return self.status()

    def test_connection(self):
        from .quota_live import locate_codex
        before=self.config_path.read_bytes() if self.config_path.exists() else b''
        self.prepare()
        health_before=self.health()
        with sqlite3.connect(self.evidence.resolve().as_uri()+'?mode=ro',uri=True) as db:
            offset=db.execute('SELECT coalesce(max(seq),0) FROM model_observations').fetchone()[0]
        _,config=self.config()
        model=config.get('model') or 'gpt-6-astra'
        with tempfile.TemporaryDirectory(prefix='cachemonitor-connection-test-') as folder:
            command=[locate_codex(),'exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
                     '--sandbox','read-only','--json','-m',model,'-c',f'openai_base_url="{self.url}"',
                     '-c','model_reasoning_effort="low"',
                     'Transport verification only. Do not use tools. Reply exactly MODEL_OBSERVER_READY.']
            process=subprocess.Popen(command,cwd=folder,env=self.verification_environment(),
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            deadline=time.monotonic()+90
            while True:
                if self.cancelled.is_set() or time.monotonic()>deadline:
                    process.kill();process.communicate()
                    raise RuntimeError('연결 시험이 취소되거나 시간이 초과됐습니다. 직접 연결은 유지됩니다.')
                try:output,_=process.communicate(timeout=.25);break
                except subprocess.TimeoutExpired:pass
        health=self.health()
        with sqlite3.connect(self.evidence.resolve().as_uri()+'?mode=ro',uri=True) as db:
            rows=db.execute("SELECT requested_model,response_model,conflict FROM model_observations WHERE seq>? AND status='completed'",(offset,)).fetchall()
        unchanged=(self.config_path.read_bytes() if self.config_path.exists() else b'')==before
        if not (unchanged and process.returncode==0 and b'MODEL_OBSERVER_READY' in output and rows and
                all(a==b==model and not conflict for a,b,conflict in rows) and health and
                health.get('instance')==health_before.get('instance') and health.get('status')=='ok' and
                health.get('relay_errors',0)==health_before.get('relay_errors',0)):
            raise RuntimeError('연결 시험이 통과하지 못했습니다. 전역 프록시는 적용하지 않았습니다.')
        state=self.state()
        state.update(phase='validated',proof_at=time.time(),proof_instance=health['instance'],proof_model=model)
        self.write_state(state)
        return self.status()

    def verification_environment(self):
        return {**os.environ,'CODEX_HOME':str(self.home)}

    def upstream(self):
        _,config=self.config()
        if config.get('model_provider','openai')!='openai':
            raise ValueError('사용자 지정 모델 공급자는 자동 연결을 지원하지 않습니다')
        current=config.get('openai_base_url')
        if current and current not in (self.url,'https://api.openai.com/v1','https://chatgpt.com/backend-api/codex'):
            raise ValueError('기존 사용자 지정 서버가 있습니다. 자동 연결로 덮어쓰지 않았습니다.')
        auth=self.home/'auth.json'
        if auth.exists():
            data=json.loads(auth.read_text(encoding='utf-8'))
            if data.get('auth_mode')=='chatgpt' or data.get('tokens'):return 'chatgpt'
        return 'openai'

    def enable(self):
        state=self.state()
        _,config=self.config()
        upstream=self.upstream()
        if not self.proof_valid(state,self.health()):
            raise RuntimeError('먼저 연결 시험을 통과해야 합니다. Codex 연결 설정은 변경하지 않았습니다.')
        if not (state.get('enabled') or state.get('pending')):
            if config.get('openai_base_url')==self.url:
                raise ValueError('기존 로컬 프록시 설정의 복원 정보가 없습니다. 먼저 기존 설정을 확인하세요.')
            state.update(home=home_key(self.home),previous_url=config.get('openai_base_url'),
                         previous_startup=startup_value(),upstream=upstream,enabled=False)
        previous_startup=startup_value()
        original_state=dict(state)
        try:
            state.update(pending=True)
            # Persist rollback information before changing shared configuration.
            self.write_state(state)
            self.task.configure(self.supervisor_command(upstream),autostart=False)
            if self.cancelled.is_set():raise RuntimeError('프록시 켜기를 취소했습니다.')
            if previous_startup==state.get('managed_startup'):startup_value(None)
            backup=self.set_url(self.url)
            state.update(enabled=True,pending=False,phase='active',upstream=upstream,url=self.url,restart_required=True,
                         last_backup=backup or state.get('last_backup'))
            self.write_state(state)
        except Exception:
            startup_value(previous_startup)
            if config.get('openai_base_url')!=self.url and self.config()[1].get('openai_base_url')==self.url:
                self.set_url(state.get('previous_url'))
            self.write_state(original_state)
            self.task.configure(self.supervisor_command(upstream),autostart=False)
            raise
        return self.status()

    def disable(self):
        return self.recover_direct()

    def recover_direct(self):
        from .app_services import disable_resume
        disable_resume(self)
        try:state=self.state()
        except (ValueError,OSError):
            if self.state_path.exists():
                atomic_write(self.directory/'backups'/('observer-state-'+uuid.uuid4().hex+'.json'),self.state_path.read_bytes())
            state={}
        # Explicit recovery must work even when enabled=False or the journal is missing.
        was_configured=self.config()[1].get('openai_base_url')==self.url
        if was_configured:
            previous=state.get('previous_url')
            if previous not in (None,'https://api.openai.com/v1','https://chatgpt.com/backend-api/codex'):previous=None
            self.set_url(previous)
        current=startup_value()
        if current and (current==state.get('managed_startup') or ('--model-proxy' in current and str(self.home) in current)):
            startup_value(state.get('previous_startup'))
        warning=None
        try:self.cleanup_legacy_check()
        except RuntimeError as exc:warning=str(exc)
        for task in (self.task,self.legacy_task):
            try:task.remove()
            except RuntimeError as exc:warning=(warning+'\n' if warning else '')+str(exc)
        state.update(home=home_key(self.home),enabled=False,pending=False,phase='off',proof_at=0,proof_instance=None,
                     restart_required=was_configured or state.get('restart_required',False),cleanup_warning=warning)
        self.write_state(state)
        # Existing sockets may still use this process. Leave it alive to drain them.
        return {**self.status(),'cleanup_warning':warning}

    def ensure(self):
        # Background polling is read-only. Only explicit actions change routing or tasks.
        result=self.status()
        if hasattr(self.task,'inspect'):
            try:result['registration']=self.task.inspect()
            except RuntimeError as exc:result['registration_issue']=str(exc)
        return result

    def turn_on(self):
        with ProcessLock(self.control_lock):
            from .app_services import require_running
            require_running(self)
            if self.config()[1].get('openai_base_url')==self.url:
                return self.attach_supervisor()
            try:
                self.test_connection()
                if self.cancelled.is_set():raise RuntimeError('프록시 켜기를 취소했습니다.')
                result=self.enable()
                if self.cancelled.is_set():raise RuntimeError('프록시 켜기를 취소했습니다.')
                return result
            except Exception as exc:
                try:self.recover_direct()
                except Exception as recovery:
                    state=self.state()
                    state.update(phase='recovery_failed',last_error=str(exc),
                                 incident={'id':uuid.uuid4().hex,'at':time.time(),'reason':str(exc),
                                           'recovery_error':str(recovery)},restart_required=True)
                    self.write_state(state)
                    raise RuntimeError(f'{exc}\n설정 복구 실패: {recovery}') from recovery
                state=self.state()
                state.update(phase='off' if self.cancelled.is_set() else 'failed',last_error=str(exc))
                self.write_state(state)
                raise

    def turn_off(self):
        with ProcessLock(self.control_lock):
            from .app_services import disable_resume
            disable_resume(self)
            return self.recover_direct()

    def update_proxy(self):
        from .proxy_update import ProxyUpdate, BUSY
        with ProcessLock(self.control_lock):
            updater=ProxyUpdate(self)
            task=ObserverTask(home_key(self.home),role='ProxyUpdate')
            current=read_json(updater.path)
            if current.get('phase') in BUSY and (task.inspect().get('state') in (2,4)
                    or time.time()-current.get('updated_at',0)<30):
                return self.status()
            prior=read_json(updater.path)
            recovering=bool(prior.get('source')) and not prior.get('restored') and prior.get('phase') in (*BUSY,'failed')
            health=self.health(timeout=3)
            if (not health and not recovering) or not updater.target.enabled():
                raise RuntimeError('실행 중인 프록시가 없습니다.')
            if not getattr(sys,'frozen',False):
                raise RuntimeError('업데이트는 설치된 배포 앱에서 실행하세요.')
            command=self.command(self.upstream() if getattr(self,'shared_cache_worker',False) else self.state().get('upstream') or self.upstream())
            command[command.index('--model-proxy')]='--proxy-update'
            if not recovering:
                atomic_write(updater.path,b'{}')
                updater.publish('queued',source_instance=health['instance'],cancel_requested=False,
                                scope=updater.target.scope,
                                message='프록시 업데이트 예약 중…')
            try:task.start(command,autostart=False)
            except Exception:
                updater.publish('failed',message='업데이트 예약 실패 · 기존 연결 유지 중');raise
        return self.status()

    def cancel_update(self):
        from .proxy_update import ProxyUpdate, BUSY
        with ProcessLock(self.control_lock):
            updater=ProxyUpdate(self)
            phase=read_json(updater.path).get('phase')
            if phase in BUSY:
                updater.publish(phase,cancel_requested=True,message='예약 취소 중…')
        return self.status()

    def update_status(self):
        from .proxy_update import BUSY
        update=read_json(self.directory/'proxy-update.json')
        if update.get('phase') in BUSY and time.time()-update.get('updated_at',0)>90:
            update={**update,'phase':'interrupted','message':'업데이트가 중단되었습니다. 다시 눌러 복구할 수 있습니다.'}
        return update

    def running_path(self,health):
        if not health or not isinstance(health.get('pid'),int):return None
        from .proxy_update import process_executable
        try:return process_executable(health['pid'])
        except OSError:return None

    def attach_supervisor(self):
        """Adopt an already configured observer without touching its live sockets."""
        from .app_services import require_running
        require_running(self)
        from .proxy_update import BUSY
        if read_json(self.directory/'proxy-update.json').get('phase') in BUSY:return self.status()
        state=self.state()
        if not state.get('enabled') or self.config()[1].get('openai_base_url')!=self.url:
            return self.status()
        upstream=state.get('upstream') or self.upstream()
        health=self.health(timeout=3)
        if health:
            # Never start a second relay or replace a live legacy supervisor.
            if health.get('lifecycle')=='managed':
                self.task.configure(self.supervisor_command(upstream),autostart=False)
            return self.status()
        self.task.start(self.supervisor_command(upstream),autostart=False)
        for _ in range(80):
            if self.cancelled.is_set():break
            runtime=self.runtime()
            if runtime.get('phase') in ('ready','active'):
                # Removing the old registration does not terminate its running instance.
                self.legacy_task.remove()
                return self.status()
            time.sleep(.1)
        raise RuntimeError('독립 감시 시작을 확인하지 못했습니다. 기존 연결은 유지됩니다.')

    def resume(self):
        from .app_services import resume_proxy
        if resume_proxy(self):return self.status()
        with ProcessLock(self.control_lock):
            if self.config()[1].get('openai_base_url')==self.url and self.state().get('enabled'):
                return self.attach_supervisor()
            return self.ensure()

    def status(self):
        try:state=self.state()
        except (ValueError,OSError):state={}
        configured=self.config()[1].get('openai_base_url')==self.url
        issue=None
        self.health_state='unknown'
        # Windows can take about two seconds to return an explicit local refusal.
        # This runs in ObserverOperation, never on the GUI thread's periodic path.
        try:health=self.health(timeout=3)
        except RuntimeError as exc:health=None;issue=str(exc)
        valid=self.proof_valid(state,health)
        phase=('active' if state.get('enabled') and health else 'recovery_required') if configured else ('validated' if valid else 'prepared' if health else 'off')
        runtime=self.runtime()
        update=self.update_status()
        if state.get('phase')=='recovery_failed':phase='recovery_failed'
        elif not configured and state.get('phase') in ('faulted','failed'):phase=state['phase']
        elif not configured and runtime.get('phase')=='draining':phase='draining'
        return {'enabled':bool(configured),'configured':configured,'running':health is not None,'validated':valid,'phase':phase,
                'health':health,'service_issue':issue,'evidence_path':str(self.evidence),
                'probe_state':self.health_state,'url':self.url,'runtime':runtime,'app_version':VERSION,
                'update':update,
                'running_proxy_path':self.running_path(health),
                'target_proxy_version':PROXY_VERSION,'proxy_update_available':bool(health and health.get('version')!=PROXY_VERSION),
                'app_path':self.command(state.get('upstream','chatgpt'))[0],
                'version_mismatch':bool(health and not proxy_compatible(health.get('version'))),
                'incident':state.get('incident'),'last_error':state.get('last_error'),
                'cleanup_warning':state.get('cleanup_warning'),
                'restart_required':bool(state.get('restart_required'))}
