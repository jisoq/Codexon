"""Signed macOS bundles and transactional, per-user installation.

Installed versions remain immutable while a GUI, relay or rollback references
them. Only the launch links and receipt change during activation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from .observer_control import atomic_write
from .observer_state import ProcessLock, read_json

BUNDLES = {'Codexon.app': ('Codexon', 'io.github.jisoq.codexon'),
           'Codexon Recovery.app': ('CodexonRecovery', 'io.github.jisoq.codexon.recovery')}
INSTALLER_ID = 'io.github.jisoq.codexon.installer'


def install_root():
    from .platform_paths import app_data_dir
    return app_data_dir() / 'installation'


def bundle_for(executable):
    path = Path(executable).resolve()
    for parent in path.parents:
        if parent.suffix == '.app' and (parent/'Contents/Info.plist').is_file():
            return parent
    return None


def manifest_for(executable):
    bundle = bundle_for(executable)
    return bundle/'Contents/Resources/build-manifest.json' if bundle else Path(executable).parent/'build-manifest.json'


def executable(bundle, name=None):
    bundle = Path(bundle)
    info = plistlib.loads((bundle/'Contents/Info.plist').read_bytes())
    binary = info.get('CFBundleExecutable')
    if not isinstance(binary, str) or Path(binary).name != binary or (name and binary != name):
        raise ValueError('앱 실행 파일 정보가 올바르지 않습니다.')
    path = bundle/'Contents/MacOS'/binary
    if not path.is_file() or not path.resolve().is_relative_to(bundle.resolve()):
        raise ValueError('앱 실행 파일을 확인하지 못했습니다.')
    return path


def verify_bundle(bundle, identifier, *, allow_ad_hoc=False, expected_team=None):
    """Verify the sealed bundle before executing any downloaded code."""
    bundle = Path(bundle).resolve(strict=True)
    binary = executable(bundle)
    info = plistlib.loads((bundle/'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != identifier:
        raise ValueError('다른 앱의 배포 파일입니다.')
    check = subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(bundle)],
                           capture_output=True, timeout=60)
    if check.returncode:
        raise ValueError('앱 서명 검증에 실패했습니다. 기존 버전을 유지합니다.')
    signed = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', str(bundle)],
                            capture_output=True, timeout=20)
    details = signed.stderr.decode('utf-8', errors='replace')
    team_match = re.search(r'^TeamIdentifier=(.+)$', details, re.MULTILINE)
    team = team_match[1] if team_match and team_match[1] != 'not set' else None
    ad_hoc = 'Signature=adhoc' in details
    if signed.returncode or (not team and not (allow_ad_hoc and ad_hoc)):
        raise ValueError('Developer ID 서명이 필요합니다. 로컬 빌드는 명시적으로 허용한 경우에만 설치합니다.')
    if expected_team and team != expected_team:
        raise ValueError('기존 앱과 업데이트의 개발자 서명이 다릅니다.')
    if team and not re.search(r'^Authority=Developer ID Application:', details, re.MULTILINE):
        raise ValueError('직접 배포용 Developer ID 서명이 아닙니다.')
    if team:
        assessment = subprocess.run(['/usr/sbin/spctl', '--assess', '--type', 'execute', str(bundle)],
                                    capture_output=True, timeout=60)
        if assessment.returncode:
            raise ValueError('macOS 배포 인증을 확인하지 못했습니다. 기존 버전을 유지합니다.')
    return dict(team=team, ad_hoc=ad_hoc, executable=str(binary),
                sha256=hashlib.sha256(binary.read_bytes()).hexdigest())


def validate_source(source, *, allow_ad_hoc=False, expected_team=None):
    source = Path(source).resolve(strict=True)
    identities = {}
    manifests = []
    for name, (binary_name, identifier) in BUNDLES.items():
        bundle = source/name
        identities[name] = verify_bundle(bundle, identifier, allow_ad_hoc=allow_ad_hoc,
                                         expected_team=expected_team)
        executable(bundle, binary_name)
        manifest = read_json(bundle/'Contents/Resources/build-manifest.json')
        if (manifest.get('product') != 'Codexon' or manifest.get('platform') != 'darwin'
                or not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d+', manifest.get('version', ''))
                or not re.fullmatch(r'[0-9a-f]{40}', manifest.get('commit', ''))
                or manifest.get('architecture') not in ('arm64', 'x86_64', 'universal2')):
            raise ValueError('macOS 배포 정보를 확인하지 못했습니다.')
        manifests.append(manifest)
    if manifests[0] != manifests[1] or len({item['team'] for item in identities.values()}) != 1:
        raise ValueError('본체와 복구 도구의 배포 정보가 다릅니다.')
    return manifests[0], identities


def installed(root=None):
    root = Path(root or install_root()).resolve()
    receipt = read_json(root/'installation.json')
    if receipt.get('platform') != 'darwin':
        return {}
    for key in ('AppPath', 'RecoveryPath'):
        path = Path(receipt.get(key, ''))
        if not path.is_absolute() or not path.is_file() or not path.resolve().is_relative_to(root/'versions'):
            return {}
    return {**receipt, 'InstallRoot': str(root)}


def _link_target(link, root):
    if not link.is_symlink():
        if link.exists():
            raise ValueError('같은 이름의 다른 앱을 보존합니다. 설치 위치를 확인해 주세요.')
        return None
    target = link.resolve()
    if not target.is_relative_to(root/'versions'):
        raise ValueError('다른 설치가 소유한 앱 링크를 보존합니다.')
    return str(target)


def _replace_link(link, target):
    if target is None:
        link.unlink(missing_ok=True)
        return
    temporary = link.with_name('.'+link.name+'.'+uuid.uuid4().hex)
    temporary.symlink_to(target, target_is_directory=True)
    try:os.replace(temporary, link)
    finally:temporary.unlink(missing_ok=True)


def _restore_activation(root, applications):
    path = root/'activation-pending.json'
    pending = read_json(path)
    if not pending:return
    if pending.get('applications') != str(applications):
        raise RuntimeError('중단된 설치의 앱 위치가 다릅니다. 기존 실행 경로를 보존합니다.')
    startup = pending.get('startup')
    if startup and startup.get('registered'):
        from .macos_startup import MacStartup
        task = MacStartup().task
        task.configure(startup['command'], autostart=startup.get('autostart', False))
        if not startup.get('enabled', True):task.suspend()
    targets = pending.get('links', {})
    if set(targets) != set(BUNDLES):
        raise RuntimeError('중단된 설치 기록을 확인하지 못했습니다.')
    for name, target in targets.items():
        if target is not None and not Path(target).resolve().is_relative_to(root/'versions'):
            raise RuntimeError('중단된 설치의 복원 경로가 올바르지 않습니다.')
        current = applications/name
        _link_target(current, root)
        _replace_link(current, target)
    prior = pending.get('receipt')
    if prior is None:(root/'installation.json').unlink(missing_ok=True)
    else:atomic_write(root/'installation.json', json.dumps(prior).encode())
    path.unlink()


def launch_gui(bundle, arguments=()):
    command=['/usr/bin/open','-n',str(bundle)]
    # LaunchServices does not inherit the caller's environment. Preserve only
    # the explicit app/QA storage overrides, never arbitrary shell credentials.
    for name in ('CODEXON_DATA_DIR','CODEXON_SERVICE_TEST_ROOT','QT_QPA_PLATFORM','CODEXON_LANGUAGE'):
        if os.environ.get(name):command+=['--env',name+'='+os.environ[name]]
    args=[argument for argument in arguments if argument!='--replace-gui']
    subprocess.run([*command,'--args',*args,'--replace-gui'],capture_output=True,check=True,timeout=20)


def install_source(source, *, root=None, applications=None, isolated=False,
                   launch=True, allow_ad_hoc=False, expected_team=None):
    root = Path(root or install_root()).resolve()
    applications = Path(applications or Path.home()/'Applications').resolve()
    manifest, identities = validate_source(source, allow_ad_hoc=allow_ad_hoc, expected_team=expected_team)
    if isolated and applications == (Path.home()/'Applications').resolve():
        raise ValueError('격리 설치에는 별도의 앱 폴더가 필요합니다.')
    root.mkdir(parents=True, exist_ok=True)
    applications.mkdir(parents=True, exist_ok=True)
    with ProcessLock(root/'install.lock', timeout=10):
        _restore_activation(root, applications)
        links = {name: _link_target(applications/name, root) for name in BUNDLES}
        previous = read_json(root/'installation.json')
        startup = None
        login = None
        if not isolated:
            from .macos_startup import MacStartup
            login = MacStartup().task
            startup = login.inspect()
            if startup.get('registered') and not startup.get('command'):
                raise RuntimeError('기존 로그인 실행 명령을 확인하지 못했습니다. 이전 설치를 유지합니다.')
        version = root/'versions'/(manifest['version']+'-'+uuid.uuid4().hex)
        version.mkdir(parents=True)
        for name in BUNDLES:
            shutil.copytree(Path(source)/name, version/name, symlinks=True)
        validate_source(version, allow_ad_hoc=allow_ad_hoc, expected_team=expected_team)
        app = executable(version/'Codexon.app', 'Codexon')
        recovery = executable(version/'Codexon Recovery.app', 'CodexonRecovery')
        report = version/'runtime.json'
        result = subprocess.run([str(app), '--verify-runtime', str(report)],
                                capture_output=True, timeout=60,
                                env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT':'1'})
        runtime = read_json(report)
        if result.returncode or runtime.get('errors') != [] or runtime.get('version') != manifest['version']:
            raise RuntimeError('새 버전 실행 검사에 실패했습니다. 이전 실행 경로를 유지합니다.')
        receipt = dict(platform='darwin', version=manifest['version'], commit=manifest['commit'],
                       architecture=manifest['architecture'], InstallRoot=str(root),
                       AppPath=str(app), RecoveryPath=str(recovery), applications=str(applications),
                       product=str(version), previous=previous.get('product'),
                       team=identities['Codexon.app']['team'], ad_hoc=identities['Codexon.app']['ad_hoc'],
                       isolated=bool(isolated), installed_at=time.time())
        pending = dict(applications=str(applications), links=links, receipt=previous or None, startup=startup)
        atomic_write(root/'activation-pending.json', json.dumps(pending).encode())
        try:
            for name in BUNDLES:_replace_link(applications/name, version/name)
            if startup and startup.get('registered'):
                login.configure([str(app), *startup['command'][1:]], autostart=startup.get('autostart', False))
                after = login.inspect()
                if after.get('command') != [str(app), *startup['command'][1:]] or after.get('autostart') != startup.get('autostart'):
                    raise RuntimeError('로그인 실행 경로 저장을 확인하지 못했습니다.')
                if not startup.get('enabled', True):login.suspend()
            atomic_write(root/'installation.json', json.dumps(receipt, ensure_ascii=False, indent=2).encode())
            if installed(root).get('AppPath') != str(app):
                raise RuntimeError('설치 경로 저장을 확인하지 못했습니다.')
        except BaseException:
            _restore_activation(root, applications)
            raise
        (root/'activation-pending.json').unlink()
        if previous:
            atomic_write(root/('installation-'+uuid.uuid4().hex+'.json'), json.dumps(previous).encode())
        if not isolated:
            # The replacement process owns its own runtime/paths and requests
            # the existing cooperative relay update. Old versions stay intact.
            connection_report = root/'connection-update.json'
            try:
                completed = subprocess.run([str(app), '--complete-install', '--control-report', str(connection_report)],
                    capture_output=True, timeout=90, env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT':'1'})
                receipt['connection'] = read_json(connection_report)
                if completed.returncode:receipt['connection']['recovery_required'] = True
            except (OSError, subprocess.TimeoutExpired):
                receipt['connection'] = dict(phase='recovery_required')
            if launch:
                from .app_services import suspended
                from .install_management import connection_manager
                if suspended(connection_manager()):launch=False
            if launch:
                # LaunchServices owns the new GUI independently of the
                # installer's job. Reconfiguring a live login job never kills it.
                launch_gui(version/'Codexon.app',startup['command'][1:] if startup and startup.get('registered') else ())
        return receipt


def prepare_uninstall(root):
    root = Path(root).resolve()
    receipt = installed(root)
    if not receipt:raise RuntimeError('이 설치의 경로를 확인하지 못했습니다.')
    with ProcessLock(root/'install.lock', timeout=5):
        if not receipt.get('isolated'):
            from .install_management import connection_manager
            from .connection_recovery import restore
            from .cache_hooks import remove_installation
            from .launch_context import resolve_homes
            manager = connection_manager()
            for home in set([str(manager.home), *resolve_homes()]):remove_installation(home, root)
            restore(manager)
        from .install_management import processes_under
        active = [p for p in processes_under(root) if p['ProcessId'] not in (os.getpid(), os.getppid())]
        if active:
            raise RuntimeError('Codexon을 종료하고 진행 중인 연결이 끝난 뒤 제거를 다시 실행하세요. 기록은 보존됩니다.')
        from .macos_services import remove_installation_tasks
        remove_installation_tasks(root)
        applications = Path(receipt['applications'])
        for name in BUNDLES:
            link = applications/name
            if _link_target(link, root) is not None:link.unlink()
        atomic_write(root/'uninstalled.json', json.dumps(receipt).encode())
        (root/'installation.json').unlink()
        return dict(ready=True, records_preserved=True, bundles_preserved=True)


def _alert(title, message, buttons, *, smoke_button=None):
    from AppKit import NSAlert, NSApplication, NSApplicationActivationPolicyRegular
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    if smoke_button is None:app.activateIgnoringOtherApps_(True)
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title);alert.setInformativeText_(message)
    for button in buttons:alert.addButtonWithTitle_(button)
    timer=None;failure=[]
    if smoke_button is not None:
        from Foundation import NSTimer, NSRunLoop
        from AppKit import NSModalPanelRunLoopMode
        def activate(timer):
            try:alert.buttons()[smoke_button].performClick_(None)
            except Exception:
                failure.append(True);app.abortModal()
        timer=NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.1,False,activate)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer,NSModalPanelRunLoopMode)
    try:choice=int(alert.runModal())-1000
    finally:
        if timer is not None:timer.invalidate()
    if failure:raise RuntimeError('The native recovery button could not be activated.')
    return choice


def installer_main(argv=None):
    parser = argparse.ArgumentParser(description='Install signed Codexon macOS bundles')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--install-root', type=Path)
    parser.add_argument('--applications', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--expected-team')
    parser.add_argument('--allow-ad-hoc', action='store_true')
    parser.add_argument('--isolated-install', action='store_true')
    parser.add_argument('--no-launch', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--detach-image', type=Path)
    args = parser.parse_args(argv)
    source = args.source
    if source is None:
        bundle = bundle_for(sys.executable)
        if not bundle:parser.error('--source is required outside the installer app')
        source = bundle/'Contents/Resources/payload'
    local = read_json(source/'Codexon.app/Contents/Resources/build-manifest.json').get('local_build') is True
    allow_ad_hoc = args.allow_ad_hoc or (local and not args.yes)
    if not args.yes:
        message = '앱과 독립 연결 복구 도구를 사용자 Applications 폴더에 설치합니다. 기존 기록은 유지됩니다.'
        if local:message += '\n\n이 패키지는 로컬 검증용 빌드이며 Apple 공증을 받지 않았습니다.'
        if _alert('Codexon 설치', message, ['설치', '취소']) != 0:return 0
    try:
        result = install_source(source, root=args.install_root, applications=args.applications,
            isolated=args.isolated_install, launch=not args.no_launch,
            allow_ad_hoc=allow_ad_hoc, expected_team=args.expected_team)
        if not args.yes:_alert('Codexon 설치 완료', 'Applications 폴더에서 Codexon과 연결 복구 도구를 열 수 있습니다.', ['확인'])
    except Exception as exc:
        result = dict(error=str(exc))
        if not args.yes:_alert('설치를 완료하지 못했습니다', str(exc), ['확인'])
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(args.report, json.dumps(result, ensure_ascii=False, indent=2).encode())
    if args.detach_image:
        # The caller copied this independent installer out of the mounted DMG.
        subprocess.run(['/usr/bin/hdiutil', 'detach', str(args.detach_image)], capture_output=True, timeout=30)
    return 1 if result.get('error') else 0


def launch_update(image, root, *, expected_version=None):
    """Mount read-only, verify identity, then copy the independent installer."""
    receipt = installed(root)
    if not receipt or not receipt.get('team'):
        raise RuntimeError('로컬 빌드는 앱 내 온라인 업데이트를 사용할 수 없습니다. 새 로컬 패키지로 설치해 주세요.')
    current = bundle_for(receipt['AppPath'])
    current_identity = verify_bundle(current, BUNDLES['Codexon.app'][1], expected_team=receipt['team'])
    directory = Path(image).parent
    mount = directory/'mounted'
    mount.mkdir()
    attached = subprocess.run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mount), str(image)],
                              capture_output=True, timeout=90)
    if attached.returncode:raise RuntimeError('업데이트 디스크 이미지를 열지 못했습니다.')
    try:
        source = mount/'Install Codexon.app'
        verify_bundle(source, INSTALLER_ID, expected_team=current_identity['team'])
        staged = directory/'Install Codexon.app'
        shutil.copytree(source, staged, symlinks=True)
        verify_bundle(staged, INSTALLER_ID, expected_team=current_identity['team'])
        manifest, _ = validate_source(staged/'Contents/Resources/payload', expected_team=current_identity['team'])
        if not expected_version or manifest['version'] != expected_version:
            raise ValueError('확인한 업데이트와 설치 파일의 버전이 다릅니다.')
        from .observer_task import ObserverTask
        command = [str(executable(staged)), '--yes', '--install-root', str(root),
                   '--applications', receipt['applications'], '--expected-team', current_identity['team'],
                   '--report', str(Path(root)/'install-result.json'), '--detach-image', str(mount)]
        ObserverTask(str(root), role='Installer').start(command)
    except BaseException:
        subprocess.run(['/usr/bin/hdiutil', 'detach', str(mount)], capture_output=True, timeout=30)
        raise
