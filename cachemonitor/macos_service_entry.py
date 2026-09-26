"""Dependency-free launchd exec entry; also staged for source development runs."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import time
import uuid


def _read(path):
    if not path.exists():
        return None
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o022:
        raise RuntimeError('Service receipt ownership changed')
    value = json.loads(path.read_text(encoding='utf-8'))
    return value if isinstance(value, dict) else None


def run_service(path):
    path = Path(path).absolute()
    if path.name != 'job.json':
        raise RuntimeError('Invalid service receipt path')
    with (path.parent / 'control.lock').open('a+b') as lock:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Service launch configuration is busy') from None
                time.sleep(.05)
        value = _read(path)
        if not value or value.get('schema') != 1:
            return 0
        if value.get('label') != path.parent.name or value.get('state_path') != str(path):
            raise RuntimeError('Service receipt path changed')
        if not value.get('enabled') or value.get('removed') or not value.get('armed', True):
            return 0
        attempts = int(value.get('attempts', 0))
        # A healthy worker may run for days. Its later failure starts a fresh
        # retry budget; only a tight crash loop is exhausted across launches.
        now = time.time()
        if now - value.get('last_launch', now) >= 300:
            attempts = 0
        exhausted = value['restart_limit'] and not value['periodic'] and attempts > value['restart_limit']
        value.update(restart_exhausted=bool(exhausted), attempts=attempts if exhausted else attempts + 1,
                     last_launch=now)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as output:
                os.chmod(temporary, 0o600)
                output.write(json.dumps(value, ensure_ascii=False).encode())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if exhausted:
            return 0
        command = value['command']
        if (not command or not all(isinstance(arg, str) and '\0' not in arg for arg in command)
                or not Path(command[0]).is_absolute() or '--launchd-service' in command):
            raise RuntimeError('Invalid worker command')
        env = os.environ.copy()
        env.update(value.get('environment', {}))
        execution_seconds = int(value.get('execution_seconds', 0))
    # POSIX preserves the alarm across exec. Only bounded periodic health
    # checks use it; update watchdogs and relays must retain their connections.
    if execution_seconds:
        signal.alarm(execution_seconds)
    try:
        os.execve(command[0], command, env)
    finally:
        if execution_seconds:
            signal.alarm(0)
    return 1


if __name__ == '__main__':
    import sys
    raise SystemExit(run_service(sys.argv[1]))
