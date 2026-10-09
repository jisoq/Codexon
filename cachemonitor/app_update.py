"""One explicit update operation, verified against one GitHub release response."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

from .installation import installed
from .version import VERSION

REPOSITORY = 'jisoq/Codexon'
HEADERS = {'Accept':'application/vnd.github+json','User-Agent':'Codexon-Updater'}
RELEASE_URL = 'https://api.github.com/repos/'+REPOSITORY+'/releases/latest'


def asset_url(value):
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or parsed.netloc!='github.com'
            or not parsed.path.startswith('/'+REPOSITORY+'/releases/download/')
            or parsed.query or parsed.fragment):
        raise ValueError('공식 배포 파일 주소가 아닙니다.')
    return value


def release_asset(release):
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('정식 배포가 아닙니다.')
    assets=release.get('assets',[])
    setups=[a for a in assets if a.get('name')=='Codexon-Setup.exe']
    hashes=[a for a in assets if a.get('name')=='Codexon-Setup.exe.sha256']
    if len(setups)!=1 or len(hashes)!=1:
        raise ValueError('이 배포에는 설치형 업데이트가 없습니다. 현재 버전은 유지됩니다.')
    for a in (setups[0],hashes[0]):asset_url(a['browser_download_url'])
    return setups[0],hashes[0]


def read_url(url, maximum=2_000_000):
    with urllib.request.urlopen(urllib.request.Request(url,headers=HEADERS),timeout=20) as response:
        data=response.read(maximum+1)
    if len(data)>maximum:raise ValueError('배포 정보가 허용 크기를 초과했습니다.')
    return data


def version_parts(value):
    if not re.fullmatch(r'v?\d{4}\.\d{2}\.\d{2}\.\d+',value):
        raise ValueError('배포 버전 형식을 확인하지 못했습니다.')
    return tuple(map(int,value.lstrip('v').split('.')))


def validated_release(value):
    """Keep only usable, same-release installer metadata; never touch local services."""
    if not isinstance(value,dict):raise ValueError('배포 정보 형식을 확인하지 못했습니다.')
    tag=value.get('tag_name')
    if not isinstance(tag,str):raise ValueError('배포 버전 형식을 확인하지 못했습니다.')
    version_parts(tag)
    try:
        setup,checksum=release_asset(value)
        prefix='https://github.com/'+REPOSITORY+'/releases/download/'+tag+'/'
        for asset in (setup,checksum):
            if asset['browser_download_url']!=prefix+asset['name']:
                raise ValueError('배포 버전과 파일 주소가 일치하지 않습니다.')
        if type(setup.get('size')) is not int or not 0<setup['size']<2_000_000_000:
            raise ValueError('설치 파일 크기가 올바르지 않습니다.')
        digest=setup.get('digest')
        if digest is not None and (not isinstance(digest,str) or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}',digest)):
            raise ValueError('설치 파일 검증 정보가 올바르지 않습니다.')
        return dict(tag_name=tag,assets=[{k:a[k] for k in ('name','browser_download_url','size','digest') if k in a}
                                         for a in (setup,checksum)])
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError('배포 정보 형식을 확인하지 못했습니다.') from exc


def launch_installer(output,root,operation_id=None):
    from .observer_task import ObserverTask
    # The installer outlives the old GUI's task/job during the handoff.
    command=[str(output),'/SILENT','/SUPPRESSMSGBOXES','/NORESTART','/LOG='+str(output.parent/'setup.log')]
    if operation_id:command.append('/UPDATEID='+operation_id)
    ObserverTask(str(root),role='Installer').start(command)


def inspect_proxy(manager):
    from .proxy_target import ProxyTarget
    from .proxy_identity import deployment
    from .version import PROXY_VERSION
    import sys
    status=manager.status()
    if not status.get('configured'):return dict(state='off',reason='프록시 사용 안 함',connections=connection_count(status))
    health=status.get('health')
    if not health:
        return dict(state='recovery' if status.get('probe_state')=='refused' else 'unknown',
                    reason='연결 복구 필요' if status.get('probe_state')=='refused' else '확인 불가',connections=None)
    target=ProxyTarget(manager)
    source=target.capture(health);command=target.replacement(source)
    distribution=deployment(sys.executable)
    current=target.ready(health,{**source,'instance':''},command,PROXY_VERSION,distribution,read_only=True)
    reason=('최신 상태' if current else '프록시 업데이트 필요' if health.get('version')!=PROXY_VERSION
            else '이전 설치본 실행 중' if source['executable_sha256']!=distribution['sha256'] or Path(source['command'][0]).resolve()!=Path(sys.executable).resolve()
            else '프록시 실행 상태 갱신 필요')
    return dict(state='current' if current else 'required',reason=reason,connections=connection_count(status),instance=health['instance'])


def check_update(progress=lambda _:None, manager=None, *, release=None):
    """Read the offered release without installing or changing the connection."""
    install=installed()
    if not install:
        raise RuntimeError('설치형 Codexon에서 업데이트할 수 있습니다. 설치 프로그램을 한 번 실행해 주세요.')
    progress('업데이트 확인 중…')
    release=validated_release(json.loads(read_url(RELEASE_URL)) if release is None else release)
    if version_parts(release['tag_name'])<=version_parts(VERSION):
        from .install_management import connection_manager
        manager=manager if manager is not None else connection_manager()
        try:proxy=inspect_proxy(manager)
        except Exception as exc:proxy=dict(state='unknown',reason='확인 불가',error=str(exc),connections=None)
        return dict(kind='proxy' if proxy['state']=='required' else 'none',manager=manager,
                    proxy=proxy,reason=proxy['reason'],connections=proxy['connections'])
    release_asset(release)
    try:status=manager.status() if manager is not None else {}
    except Exception:status={}
    return dict(kind='app',release=release,install=install,manager=manager,connections=connection_count(status),connection_enabled=status.get('configured'))


def connection_count(status):
    count=(status.get('health') or {}).get('active_connections')
    return count if type(count) is int and count>=0 else None


def update(progress=lambda _:None, manager=None, *, plan=None):
    # The GUI passes the exact offer the user confirmed; do not fetch a newer
    # release between confirmation and installation.
    plan=check_update(progress,manager) if plan is None else plan
    if plan['kind']=='none':return '설치할 새 버전이 없습니다.'
    from .update_state import state_for,scope_for
    from .observer_state import ProcessLock
    from .install_management import connection_manager
    manager=plan.get('manager') or manager or connection_manager()
    install=plan.get('install') or installed()
    if getattr(sys,'frozen',False) and install and Path(install.get('AppPath','')).resolve()!=Path(sys.executable).resolve():
        raise RuntimeError('시작 메뉴에서 새 Codexon을 열어 업데이트를 계속하세요.')
    journal=state_for(manager,install)
    # Cross-window and cross-process single flight, including download and launch.
    journal.root.mkdir(parents=True,exist_ok=True)
    with ProcessLock(journal.root/'app-update-worker.lock'):
        if plan['kind']=='proxy':
            current=inspect_proxy(manager)
            if current['state']=='current':return '모두 최신 상태'
            if current['state']!='required' or current.get('instance')!=plan.get('proxy',{}).get('instance'):
                raise RuntimeError('연결 상태가 바뀌었습니다. 업데이트를 다시 확인하세요.')
        record=journal.begin(plan['kind'],plan['release']['tag_name'].lstrip('v') if plan['kind']=='app' else VERSION,
                             scope=scope_for(manager),release=plan.get('release'))
        operation_id=record['operation_id']
        try:
            if plan['kind']=='proxy':
                from .install_management import activate_proxy
                result=activate_proxy(manager,operation_id=operation_id,update_root=journal.root)
                journal.proxy_result(operation_id,result)
            else:
                download_and_install(plan['release'],install,progress,journal,operation_id)
        except UpdateCancelled:
            journal.change(operation_id,phase='cancelled',message='업데이트를 취소했습니다.')
        except Exception as exc:
            journal.fail(operation_id,str(exc))
            raise
        return journal.read()


class UpdateCancelled(Exception):
    pass


def download_and_install(release,install,progress,journal,operation_id):
    def allowed():
        if journal.cancelled(operation_id):raise UpdateCancelled()
    allowed()
    journal.change(operation_id,phase='downloading')
    asset,checksum=release_asset(release)
    text=read_url(checksum['browser_download_url'],1024).decode('ascii').strip()
    allowed()
    match=re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?Codexon-Setup\.exe',text)
    if not match:raise ValueError('설치 파일 검증 정보가 올바르지 않습니다.')
    expected=match[1].lower()
    size=asset.get('size')
    if type(size) is not int or not 0<size<2_000_000_000:raise ValueError('설치 파일 크기가 올바르지 않습니다.')
    directory=Path(install['InstallRoot'])/'downloads'/uuid.uuid4().hex
    directory.mkdir(parents=True)
    partial=directory/'Codexon-Setup.partial'
    output=directory/'Codexon-Setup.exe'
    progress('새 버전 다운로드 중…')
    digest=hashlib.sha256();total=0;deadline=time.monotonic()+600
    try:
        with urllib.request.urlopen(urllib.request.Request(asset['browser_download_url'],headers=HEADERS),timeout=20) as response, partial.open('xb') as file:
            while chunk:=response.read(1024*1024):
                allowed()
                total+=len(chunk)
                if total>size or time.monotonic()>deadline:raise ValueError('다운로드가 완료되지 않았습니다.')
                file.write(chunk);digest.update(chunk)
        if total!=size or digest.hexdigest()!=expected or (asset.get('digest') and asset['digest']!='sha256:'+expected):
            raise ValueError('설치 파일 검증에 실패했습니다. 현재 버전을 유지합니다.')
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    allowed()
    if not journal.install_ready(operation_id,output,expected):raise UpdateCancelled()
    progress('새 버전 설치 중…')
    launch_installer(output,install['InstallRoot'],operation_id)


def retry_proxy(manager,journal):
    """Resume only the outstanding connection stage of the same operation."""
    from .install_management import activate_proxy
    from .update_state import scope_for
    from .observer_state import ProcessLock
    with ProcessLock(journal.root/'app-update-worker.lock'):
        record=journal.read();operation_id=record.get('operation_id')
        if not operation_id or record.get('phase') not in ('partial','failed','deferred','interrupted'):
            raise RuntimeError('다시 적용할 연결 작업이 없습니다.')
        if record.get('scope')!=scope_for(manager):raise RuntimeError('다른 연결의 업데이트 기록을 보존합니다.')
        if record.get('app',{}).get('state') not in ('verified','current'):
            raise RuntimeError('앱 설치가 완료되지 않았습니다. 업데이트를 다시 확인하세요.')
        if getattr(sys,'frozen',False) and record.get('app',{}).get('state')=='verified' and Path(record['app']['executable']).resolve()!=Path(sys.executable).resolve():
            raise RuntimeError('시작 메뉴에서 새 Codexon을 열어 업데이트를 계속하세요.')
        journal.change(operation_id,phase='waiting',cancel_requested=False,defer_requested=False)
        try:
            result=activate_proxy(manager,operation_id=operation_id,update_root=journal.root)
            journal.proxy_result(operation_id,result)
        except Exception as exc:
            journal.fail(operation_id,str(exc));raise
        return journal.read()


def reconcile_update(journal,manager=None):
    """An expired timer is not failure; prove the owning process/task has ended."""
    from .proxy_identity import same_process
    from .observer_task import ObserverTask
    record=journal.read();phase=record.get('phase')
    previous=record.get('proxy',{})
    if manager and phase in ('waiting','needs_exit') and previous.get('source_instance') and not previous.get('operation_id'):
        from .proxy_update import BUSY
        from .update_state import scope_for
        if record.get('scope')!=scope_for(manager):return record
        current=manager.update_status()
        if current.get('source_instance')!=previous['source_instance'] or current.get('operation_id'):return record
        if current.get('phase') in BUSY:
            health=manager.health(timeout=3) or {};unknown=(health.get('websocket_states') or {}).get('unknown',0)
            current={**current,'required_action':'close_client' if unknown else None,'unknown_connections':unknown,
                     'responding':(health.get('websocket_states') or {}).get('responding',0)+health.get('http_connections',0)}
            journal.proxy_result(record['operation_id'],current)
        elif current.get('phase') in ('complete','failed','cancelled','interrupted'):
            if record.get('cancel_requested'):
                journal.proxy_result(record['operation_id'],dict(phase='cancelled'))
            else:
                # A worker from the previous app may only have applied its own
                # old deployment. Queue the new target and let it prove readiness.
                from .install_management import activate_proxy
                try:
                    result=activate_proxy(manager,operation_id=record['operation_id'],update_root=journal.root)
                    journal.proxy_result(record['operation_id'],result)
                except Exception as exc:journal.fail(record['operation_id'],str(exc))
        return journal.read()
    if time.time()-record.get('updated_at',0)<90:return record
    try:
        if phase in ('preparing','downloading'):
            if not record.get('owner') or same_process(record['owner']):return record
            journal.change(record['operation_id'],phase='interrupted',message='업데이트 준비가 중단되었습니다. 다시 확인하세요.')
        elif phase=='installing':
            task=ObserverTask(str(journal.root),role='Installer').inspect()
            if task.get('registered') and (task.get('running') or task.get('state') in (2,4)):return record
            journal.fail(record['operation_id'],'설치 완료를 확인하지 못했습니다. 업데이트를 다시 확인하세요.')
    except (OSError,RuntimeError):return record
    return journal.read()
