from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
import stat
from pathlib import Path
import threading
import tempfile


DIAGNOSTIC_EXIT = 86
_PREFIX = "[codexon-check] "
_first_error = None
_notify = True
_event_log = None
_lock = threading.Lock()
try:
    _stdout = os.dup(1)
except OSError:
    _stdout = None


@dataclass(frozen=True)
class DiagnosticError:
    operation: str
    path: str
    exception_type: str
    message: str
    pid: int


class DiagnosticFailure(Exception):
    pass


def reset():
    global _first_error
    _first_error = None


def status():
    return {'first_error': _first_error}


def write_output(descriptor, value):
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError('stdout accepted no output bytes')
        view = view[written:]


def claim_event_log():
    global _event_log
    _event_log = os.environ.pop('CODEXON_CHECK_EVENT_LOG', None)


def emit(value, *, strict=False, blocking=True):
    line = (_PREFIX + json.dumps(value, ensure_ascii=True) + "\n").encode()
    try:
        if _event_log:
            with open(_event_log, 'ab', buffering=0) as output:
                write_output(output.fileno(), line)
        if _stdout is None:
            raise OSError('original stdout is unavailable')
        was_blocking = None
        try:
            if not blocking and stat.S_ISFIFO(os.fstat(_stdout).st_mode):
                was_blocking = os.get_blocking(_stdout)
                os.set_blocking(_stdout, False)
            write_output(_stdout, line)
        finally:
            if was_blocking is not None:
                os.set_blocking(_stdout, was_blocking)
    except OSError:
        if strict:
            raise


@contextmanager
def capture_stdout():
    with diagnostic_io('create subprocess capture', Path(tempfile.gettempdir())):
        temporary = tempfile.TemporaryDirectory(prefix='codexon-check-output-')
    try:
        capture = Path(temporary.name) / 'stdout.log'
        with diagnostic_io('open subprocess capture', capture):
            output = capture.open('wb', buffering=0)
        with output:
            yield capture, output
    finally:
        try:
            with diagnostic_io('remove subprocess capture', Path(temporary.name)):
                temporary.cleanup()
        except DiagnosticFailure:
            pass


def retain(error):
    global _first_error
    with _lock:
        if _first_error is None and error is not None:
            _first_error = error
            if _notify:
                emit({'diagnostics': status()})


@contextmanager
def defer_notifications():
    global _notify
    previous = _notify
    _notify = False
    try:
        yield
    except BaseException:
        if status()['first_error']:
            emit(dict(product_exit_code=None, diagnostics=status()), blocking=False)
        raise
    finally:
        _notify = previous


@contextmanager
def diagnostic_io(operation, path):
    try:
        yield
    except (OSError, UnicodeError, ValueError) as error:
        retain(asdict(DiagnosticError(operation, str(Path(path).resolve()),
                                     type(error).__name__, str(error), os.getpid())))
        raise DiagnosticFailure(str(error)) from error


def write_json(path, value):
    with diagnostic_io('write JSON', path):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def read_json(path):
    with diagnostic_io('read JSON', path):
        return json.loads(path.read_text(encoding='utf-8-sig'))


def exit_code(product_exit_code):
    return product_exit_code if product_exit_code else DIAGNOSTIC_EXIT if _first_error else 0


class CheckOutput:
    def __init__(self):
        self.first_error = None
        self.product_exit_code = None
        self.has_outcome = False
        self.pending = ''
        self.active_test = None

    def feed(self, chunk, *, final=False):
        combined = self.pending + chunk
        lines = combined.split('\n')
        self.pending = '' if final else lines.pop()
        incomplete = len(lines)-1 if final and not combined.endswith('\n') else -1
        for index, line in enumerate(lines):
            if _PREFIX not in line:
                continue
            try:
                value = json.loads(line.split(_PREFIX, 1)[1])
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            diagnostics = value.get('diagnostics', {})
            if not isinstance(diagnostics, dict):
                continue
            error = diagnostics.get('first_error')
            if error is not None and (not isinstance(error, dict)
                    or not all(isinstance(error.get(key), str) for key in ('operation', 'path', 'exception_type', 'message'))
                    or type(error.get('pid')) is not int):
                continue
            if 'product_exit_code' in value and value['product_exit_code'] is not None and type(value['product_exit_code']) is not int:
                continue
            if index != incomplete and isinstance(value.get('active_test'), dict):
                self.active_test = value['active_test']
            if self.first_error is None and error:
                self.first_error = error
            if 'product_exit_code' in value:
                if value['product_exit_code'] is not None or not self.has_outcome:
                    self.product_exit_code = value['product_exit_code']
                self.has_outcome = True
