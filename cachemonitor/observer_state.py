"""Cross-process observer coordination, scoped to one app-owned data directory."""
import json
import os
from pathlib import Path
import time


def read_json(path, default=None):
    for attempt in range(6):
        try:
            value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            return value if isinstance(value, dict) else (default or {})
        except OSError as error:
            code = getattr(error, 'winerror', None)
            if (os.name != 'nt' or attempt == 5 or not (code in (5, 32, 33)
                    or code is None and isinstance(error, PermissionError) and error.errno == 13)):
                return default or {}
            time.sleep(.02 * (attempt + 1))
        except ValueError:
            return default or {}


class ProcessLock:
    def __init__(self, path, timeout=0):
        self.path, self.timeout, self.file = Path(path), timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0'); self.file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            self.file.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close(); self.file = None
                    raise RuntimeError('다른 프록시 설정 작업이 진행 중입니다.')
                time.sleep(.05)

    def __exit__(self, *_):
        if self.file:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close(); self.file = None
