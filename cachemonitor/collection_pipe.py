"""Private Windows byte pipes with owned, cancellable overlapped operations."""
import ctypes
from ctypes import wintypes
import math
import secrets
import threading
import time
import _winapi


_PIPE_EOF = (_winapi.ERROR_BROKEN_PIPE, _winapi.ERROR_NO_DATA, 233)  # ERROR_PIPE_NOT_CONNECTED
_PIPE_REJECT_REMOTE_CLIENTS = 8
_SECURITY_SQOS_PRESENT = 0x100000
_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_advapi = ctypes.WinDLL('advapi32', use_last_error=True)
_kernel.GetCurrentProcess.restype = wintypes.HANDLE
_kernel.LocalFree.argtypes = [ctypes.c_void_p]
_kernel.LocalFree.restype = ctypes.c_void_p
_kernel.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
_kernel.DisconnectNamedPipe.restype = wintypes.BOOL
_advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_advapi.OpenProcessToken.restype = wintypes.BOOL
_advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_advapi.GetTokenInformation.restype = wintypes.BOOL
_advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
_advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
_advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p),
                ('inherit', wintypes.BOOL)]


def _security():
    """Only this user, SYSTEM and administrators may open a pipe instance."""
    token = wintypes.HANDLE()
    if not _advapi.OpenProcessToken(_kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    sid_text = ctypes.c_void_p()
    try:
        size = wintypes.DWORD()
        _advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        value = ctypes.create_string_buffer(size.value)
        if not _advapi.GetTokenInformation(token, 1, value, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(value, ctypes.POINTER(ctypes.c_void_p))[0]
        if not _advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = 'D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;' + ctypes.wstring_at(sid_text) + ')'
        descriptor = ctypes.c_void_p()
        if not _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    finally:
        if sid_text.value:
            _kernel.LocalFree(sid_text)
        _winapi.CloseHandle(token.value)


class PipeStream:
    """One owner performs I/O and closes; another thread may only cancel it."""
    def __init__(self, handle, *, server=False):
        self.handle = handle
        self.server = server
        self.timeout = None
        self.lock = threading.Lock()
        self.operation = None
        self.cancelled = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def settimeout(self, timeout):
        self.timeout = timeout

    def _perform(self, start, *, reading=False):
        with self.lock:
            if self.cancelled or self.handle is None:
                raise OSError('Collection pipe cancelled')
            try:
                operation, error = start(self.handle)
            except OSError as error:
                if reading and error.winerror in _PIPE_EOF:
                    return b''
                raise
            self.operation = operation
        try:
            if error == _winapi.ERROR_IO_PENDING:
                milliseconds = (_winapi.INFINITE if self.timeout is None else
                                max(1, math.ceil(self.timeout * 1000)))
                if _winapi.WaitForSingleObject(operation.event, milliseconds) == _winapi.WAIT_TIMEOUT:
                    raise TimeoutError('Collection pipe deadline expired')
            try:
                count, error = operation.GetOverlappedResult(True)
            except OSError as error:
                if reading and error.winerror in _PIPE_EOF:
                    return b''
                raise
            if error:
                if reading and error in _PIPE_EOF:
                    return b''
                raise ctypes.WinError(error)
            return bytes(operation.getbuffer()) if reading else count
        finally:
            # Cancellation does not release its buffer or handle. Reap it on
            # the I/O owner before either can be reused by another operation.
            with self.lock:
                operation.cancel()
                try:
                    operation.GetOverlappedResult(True)
                except OSError:
                    pass
                finally:
                    self.operation = None

    def send(self, value):
        return self._perform(lambda handle: _winapi.WriteFile(handle, value, overlapped=True))

    def recv(self, size):
        if not size:
            return b''
        return self._perform(lambda handle: _winapi.ReadFile(handle, size, overlapped=True), reading=True)

    def connect(self):
        def begin(handle):
            operation = _winapi.ConnectNamedPipe(handle, overlapped=True)
            return operation, _winapi.ERROR_IO_PENDING
        self._perform(begin)

    def shutdown(self, _how=None):
        with self.lock:
            self.cancelled = True
            if self.operation is not None:
                self.operation.cancel()
            if self.server and self.handle is not None:
                # Break a peer's wait even while our callback is finishing.
                # The owner still reaps pending I/O before closing the handle.
                _kernel.DisconnectNamedPipe(self.handle)

    def close(self):
        with self.lock:
            if self.operation is not None:
                raise RuntimeError('Collection pipe I/O must finish before close')
            if self.handle is not None:
                _winapi.CloseHandle(self.handle)
                self.handle = None


class PipeListener:
    """At most sixteen handlers plus one waiting instance; no TCP ports."""
    def __init__(self):
        self.address = r'\\.\pipe\Codexon-collection-' + secrets.token_hex(16)
        self.lock = threading.Lock()
        self.closed = False
        self.accepting = False
        self.pending = None
        self.security = _security()
        try:
            self.pending = self._new(first=True)
        except BaseException:
            _kernel.LocalFree(self.security.descriptor)
            raise

    def _new(self, *, first=False):
        flags = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
        if first:
            flags |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
        handle = _winapi.CreateNamedPipe(self.address, flags,
            _PIPE_REJECT_REMOTE_CLIENTS, 17, 64 * 1024, 64 * 1024, 0, ctypes.addressof(self.security))
        return PipeStream(handle, server=True)

    def prepare(self):
        with self.lock:
            if self.closed:
                raise OSError('Collection pipe listener closed')
            if self.pending is None:
                self.pending = self._new()

    def accept(self):
        while True:
            try:
                return self._accept()
            except OSError as error:
                # A client can disappear between CreateFile and our pending
                # ConnectNamedPipe. Retire only that instance, not the server.
                if self.closed or error.winerror not in _PIPE_EOF:
                    raise

    def _accept(self):
        self.prepare()
        with self.lock:
            if self.closed:
                raise OSError('Collection pipe listener closed')
            stream = self.pending
            self.accepting = True
        try:
            stream.connect()
        except BaseException:
            with self.lock:
                self.pending = None
            stream.close()
            raise
        finally:
            with self.lock:
                self.accepting = False
        with self.lock:
            self.pending = None
            if self.closed:
                stream.close()
                raise OSError('Collection pipe listener closed')
        return stream, None

    def close(self):
        with self.lock:
            self.closed = True
            if self.pending is not None:
                self.pending.shutdown()
                if not self.accepting:
                    self.pending.close()
                    self.pending = None
            if self.security.descriptor:
                _kernel.LocalFree(self.security.descriptor)
                self.security.descriptor = None


def connect(address, deadline, cancel=None):
    """A busy instance shares the caller's short connection deadline."""
    while True:
        if cancel is not None and cancel.closed:
            raise OSError('Collection request cancelled')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Collection pipe connection deadline expired')
        try:
            handle = _winapi.CreateFile(address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0, 0, _winapi.OPEN_EXISTING,
                _winapi.FILE_FLAG_OVERLAPPED | _SECURITY_SQOS_PRESENT, 0)  # Anonymous impersonation level.
            return PipeStream(handle)
        except OSError as error:
            if error.winerror != _winapi.ERROR_PIPE_BUSY:
                raise
        try:
            _winapi.WaitNamedPipe(address, max(1, min(50, math.ceil(remaining * 1000))))
        except OSError as error:
            if error.winerror != _winapi.ERROR_SEM_TIMEOUT:
                raise
