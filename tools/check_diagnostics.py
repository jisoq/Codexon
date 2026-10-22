from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import threading


DIAGNOSTIC_EXIT = 86
_PREFIX = "[codexon-check] "
_first_error = None
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


def emit(value):
    line = (_PREFIX + json.dumps(value, ensure_ascii=True) + "\n").encode()
    if _stdout is not None:
        try:
            os.write(_stdout, line)
        except OSError:
            pass


def retain(error):
    global _first_error
    with _lock:
        if _first_error is None and error is not None:
            _first_error = error
            emit({'diagnostics': status()})


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

    def feed(self, chunk, *, final=False):
        lines = (self.pending + chunk).split('\n')
        self.pending = '' if final else lines.pop()
        for line in lines:
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
            if self.first_error is None and error:
                self.first_error = error
            if 'product_exit_code' in value:
                if value['product_exit_code'] is not None or not self.has_outcome:
                    self.product_exit_code = value['product_exit_code']
                self.has_outcome = True
