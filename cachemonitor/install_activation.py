"""Recoverable activation of installed launch pointers, without importing Qt."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import winreg

from .observer_control import atomic_write


def names(isolated=False):
    product = 'Codexon-QA' if isolated else 'Codexon'
    protocol = 'codexon-recovery-qa' if isolated else 'codexon-recovery'
    return product, protocol


def programs():
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 2, None, 0, buffer):
        raise OSError('Cannot locate the user Start menu')
    return Path(buffer.value)


def shortcuts(isolated=False):
    label = 'Codexon QA' if isolated else 'Codexon'
    return [programs()/(label+suffix+'.lnk') for suffix in ('', ' 연결 복구', ' Connection Recovery')]


def registry_slots(isolated=False):
    product, protocol = names(isolated)
    slots = [(rf'Software\{product}', name) for name in ('InstallRoot', 'AppPath', 'RecoveryPath')]
    slots += [(rf'Software\Classes\{protocol}', ''), (rf'Software\Classes\{protocol}', 'URL Protocol'),
              (rf'Software\Classes\{protocol}\shell\open\command', ''),
              (rf'Software\Classes\AppUserModelId\{product}.Recovery', 'DisplayName')]
    if not isolated:slots.append((r'Software\Microsoft\Windows\CurrentVersion\Run', 'CacheMonitor'))
    return slots


def snapshot_registry(isolated):
    values = []
    for path, name in registry_slots(isolated):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:value = winreg.QueryValueEx(key, name)
        except FileNotFoundError:value = None
        values.append([path, name, value])
    return values


def restore_registry(values):
    for path, name, value in values:
        if value is None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                    try:winreg.DeleteValue(key, name)
                    except FileNotFoundError:pass
            except FileNotFoundError:pass
        else:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                winreg.SetValueEx(key, name, 0, value[1], value[0])


class Activation:
    def __init__(self, root, isolated):
        self.root = Path(root)
        self.journal = self.root/'activation-pending.json'
        if self.journal.exists():
            self.state = json.loads(self.journal.read_text(encoding='utf-8'))
            self.rollback()
        paths = [self.root/'installation.json', *shortcuts(isolated)]
        if not isolated:
            from .launch_context import preference_path
            from .installation import pointer_path
            paths.extend([preference_path(),pointer_path()])
        self.state = dict(registry=snapshot_registry(isolated), files=[
            [str(p), base64.b64encode(p.read_bytes()).decode() if p.exists() else None] for p in paths])
        atomic_write(self.journal, json.dumps(self.state, ensure_ascii=False).encode())

    def rollback(self):
        errors = []
        try:restore_registry(self.state['registry'])
        except OSError as exc:errors.append(str(exc))
        for path, data in self.state['files']:
            try:
                expected=self.state.get('guards',{}).get(path)
                if expected is not None:
                    current=base64.b64encode(Path(path).read_bytes()).decode() if Path(path).exists() else None
                    if current not in (expected,data):raise OSError('Concurrent edit preserved: '+path)
                if data is None:Path(path).unlink(missing_ok=True)
                else:
                    old = base64.b64decode(data)
                    if not Path(path).exists() or Path(path).read_bytes()!=old:atomic_write(Path(path), old)
            except OSError as exc:errors.append(str(exc))
        if errors:raise RuntimeError('Activation rollback needs retry: '+'; '.join(errors))
        self.journal.unlink(missing_ok=True)

    def track_file(self, path, expected):
        path=Path(path)
        if any(saved==str(path) for saved,_ in self.state['files']):return
        self.state['files'].append([str(path),base64.b64encode(path.read_bytes()).decode() if path.exists() else None])
        self.state.setdefault('guards',{})[str(path)]=base64.b64encode(expected).decode()
        atomic_write(self.journal,json.dumps(self.state,ensure_ascii=False).encode())

    def commit(self):
        self.journal.unlink()


def publish_shell(product, recovery, *, isolated=False, language='ko'):
    name, protocol = names(isolated)
    label = ('Codexon QA' if isolated else 'Codexon') + (' Connection Recovery' if language=='en' else ' 연결 복구')
    values = [(rf'Software\Classes\{protocol}', '', 'URL:Codexon connection recovery'),
              (rf'Software\Classes\{protocol}', 'URL Protocol', ''),
              (rf'Software\Classes\{protocol}\shell\open\command', '', f'"{recovery}" "%1"'),
              (rf'Software\Classes\AppUserModelId\{name}.Recovery', 'DisplayName', label)]
    for path, field, value in values:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, field, 0, winreg.REG_SZ, value)
    links = shortcuts(isolated)
    selected = links[2] if language=='en' else links[1]
    entries = [dict(path=str(links[0]), target=str(product/'Codexon.exe'), cwd=str(product)),
               dict(path=str(selected), target=str(recovery), cwd=str(recovery.parent))]
    encoded = base64.b64encode(json.dumps(entries).encode()).decode()
    script = """
$ErrorActionPreference='Stop'
$entries=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__DATA__')))
$shell=New-Object -ComObject WScript.Shell
foreach ($entry in $entries) {
  $link=$shell.CreateShortcut($entry.path)
  $link.TargetPath=$entry.target
  $link.WorkingDirectory=$entry.cwd
  $link.Save()
  $verified=$shell.CreateShortcut($entry.path)
  if($verified.TargetPath -ne $entry.target -or $verified.WorkingDirectory -ne $entry.cwd){throw 'Shortcut verification failed'}
}
""".replace('__DATA__', encoded)
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
        base64.b64encode(script.encode('utf-16-le')).decode()], capture_output=True,
        timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise OSError('Cannot update Start menu shortcuts')
    for path,field,value in values:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,path) as key:
            if winreg.QueryValueEx(key,field)!=(value,winreg.REG_SZ):
                raise OSError('Shell registry verification failed')
    from .shell_shortcut import application_id
    application_id(selected,name+'.Recovery')
    for path in links[1:]:
        if path!=selected:path.unlink(missing_ok=True)


def remove_shortcuts(isolated=False):
    for path in shortcuts(isolated):path.unlink(missing_ok=True)
