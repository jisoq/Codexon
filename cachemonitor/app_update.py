"""One explicit update operation, verified against one GitHub release response."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

from .installation import installed
from .version import VERSION

REPOSITORY = 'jisoq/Codexon'
HEADERS = {'Accept':'application/vnd.github+json','User-Agent':'Codexon-Updater'}


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


def launch_installer(output,root):
    from .observer_task import ObserverTask
    # The installer outlives the old GUI's task/job during the handoff.
    ObserverTask(str(root),role='Installer').start([str(output),'/SILENT','/SUPPRESSMSGBOXES','/NORESTART',
                                                  '/LOG='+str(output.parent/'setup.log')])


def update(progress=lambda _:None):
    install=installed()
    if not install:
        raise RuntimeError('설치형 Codexon에서 업데이트할 수 있습니다. 설치 프로그램을 한 번 실행해 주세요.')
    progress('업데이트 확인 중…')
    release=json.loads(read_url('https://api.github.com/repos/'+REPOSITORY+'/releases/latest'))
    if version_parts(release['tag_name'])<=version_parts(VERSION):
        from .install_management import activate_proxy
        from .connection_recovery import target
        state=activate_proxy(target())
        return state.get('message') or '최신 버전입니다.'
    asset,checksum=release_asset(release)
    text=read_url(checksum['browser_download_url'],1024).decode('ascii').strip()
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
                total+=len(chunk)
                if total>size or time.monotonic()>deadline:raise ValueError('다운로드가 완료되지 않았습니다.')
                file.write(chunk);digest.update(chunk)
        if total!=size or digest.hexdigest()!=expected or (asset.get('digest') and asset['digest']!='sha256:'+expected):
            raise ValueError('설치 파일 검증에 실패했습니다. 현재 버전을 유지합니다.')
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    progress('새 버전 설치 중…')
    # The user's click authorizes installation. Inno never closes applications itself.
    launch_installer(output,install['InstallRoot'])
    return '업데이트 설치를 시작했습니다. 연결이 끝나면 프록시도 적용됩니다.'
