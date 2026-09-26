"""Locate the installed Codex runtime without depending on an interactive shell."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


def locate_codex():
    explicit = os.environ.get('CODEXON_CODEX_PATH')
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file() and (os.name == 'nt' or os.access(path, os.X_OK)):
            return str(path.resolve())
        raise RuntimeError('설정한 Codex 실행 파일을 사용할 수 없습니다. 실행 경로를 확인하세요.')
    if os.name == 'nt':
        candidates = []
        direct = shutil.which('codex.exe')
        if direct:
            candidates.append(Path(direct))
        root = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'OpenAI' / 'Codex' / 'bin'
        candidates.extend(root.glob('*/codex.exe'))
        npm = Path(os.environ.get('APPDATA', Path.home())) / 'npm' / 'node_modules' / '@openai'
        candidates.extend(npm.glob('codex*/**/codex.exe'))
        candidates = [path for path in candidates if path.is_file()]
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime))
    else:
        direct = shutil.which('codex')
        candidates = [Path(direct)] if direct else []
        candidates.extend(root / 'codex' for root in (
            Path.home() / '.local' / 'bin', Path('/opt/homebrew/bin'), Path('/usr/local/bin')))
        if sys.platform == 'darwin':
            for root in (Path('/Applications'), Path.home() / 'Applications'):
                for name in ('Codex.app', 'ChatGPT.app'):
                    resources = root / name / 'Contents' / 'Resources'
                    for relative in ('codex', 'bin/codex', 'codex/bin/codex',
                                     'app.asar.unpacked/node_modules/@openai/codex/bin/codex'):
                        candidates.append(resources / relative)
                    candidates.extend(resources.glob('codex-*/codex'))
        for path in candidates:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.resolve())
    raise RuntimeError('Codex 실행 파일을 찾을 수 없습니다. Codex를 설치하거나 CODEXON_CODEX_PATH를 설정하세요. 로컬 기록은 계속 확인할 수 있습니다.')


def runtime_environment(home):
    env = os.environ.copy()
    env['CODEX_HOME'] = str(home)
    if sys.platform == 'darwin':
        paths = [*env.get('PATH', '').split(os.pathsep), str(Path.home() / '.local' / 'bin'),
                 '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin']
        env['PATH'] = os.pathsep.join(dict.fromkeys(path for path in paths if path))
    return env
