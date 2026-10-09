"""Fatal relay diagnostics that do not depend on a functioning event loop."""
from collections import deque
from pathlib import Path
import asyncio
import json
import os
import time
import traceback
import uuid

from .observer_control import atomic_write
from .version import PROXY_VERSION


def error_details(error):
    # Exception messages, source lines and locals may contain request secrets.
    codes={name:value if isinstance(value:=getattr(error, name, None), int) else None
           for name in ('errno', 'winerror')}
    return dict(type=type(error).__name__, **codes,
                frames=[dict(file=Path(frame.filename).name, line=frame.lineno, function=frame.name)
                        for frame in traceback.extract_tb(error.__traceback__)[-24:]])


class ProxyDiagnostics:
    def __init__(self, directory):
        self.path = Path(directory) / 'proxy-failure.json'
        self.events = deque(maxlen=64)
        self.snapshot = lambda: {}
        self.failure = None
        self.event_loop = None

    def event(self, connection, phase):
        self.events.append(dict(at=time.time(), connection=connection, phase=phase))

    def fail(self, error, phase):
        details = error_details(error)
        if self.failure is None:
            try:
                snapshot = self.snapshot()
            except Exception as snapshot_error:
                snapshot = dict(snapshot_error=error_details(snapshot_error))
            self.failure = dict(id=uuid.uuid4().hex, at=time.time(), pid=os.getpid(),
                                version=PROXY_VERSION, event_loop=self.event_loop, phase=phase,
                                error=details, snapshot=snapshot, events=list(self.events), cleanup_errors=[])
            try:
                if self.path.exists():
                    atomic_write(self.path.with_name('proxy-failure.previous.json'), self.path.read_bytes())
            except OSError:
                pass
        else:
            self.failure['cleanup_errors'].append(dict(phase=phase, **details))
        self.save()

    def save(self, **fields):
        if self.failure is None:
            return
        self.failure.update(fields)
        try:
            atomic_write(self.path, json.dumps(self.failure, ensure_ascii=True, indent=2).encode())
        except OSError:
            # A full/read-only disk must not turn a background failure into a modal dialog.
            pass


def run_loop(serve, diagnostics, loop_factory):
    """Do not poll a broken selector again, or let PyInstaller show an error dialog."""
    loop = None
    task = None
    try:
        loop = loop_factory()
        diagnostics.event_loop = type(loop).__name__
        task = loop.create_task(serve())
        try:
            loop.run_until_complete(task)
        except KeyboardInterrupt:
            task.cancel()
            loop.run_until_complete(asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor(timeout=5))
    except Exception as error:
        diagnostics.fail(error, 'event_loop')
    finally:
        if loop is not None:
            try:
                loop.close()
            except Exception as error:
                diagnostics.fail(error, 'loop_close')
    return 1 if diagnostics.failure else 0
