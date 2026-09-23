"""Read the embedded installer identity before allowing a QA executable to run."""
import ctypes
from ctypes import wintypes as W
from pathlib import Path


def require_qa_installer(path):
    path = str(Path(path).resolve())
    api = ctypes.WinDLL('version', use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [W.LPCWSTR, ctypes.POINTER(W.DWORD)]
    api.GetFileVersionInfoW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p]
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(W.UINT)]
    size = api.GetFileVersionInfoSizeW(path, None)
    if not size:raise ValueError('Installer has no readable QA identity')
    data = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(path, 0, size, data):raise ValueError('Cannot read installer identity')
    pointer = ctypes.c_void_p();length = W.UINT()
    if not api.VerQueryValueW(data, r'\VarFileInfo\Translation', ctypes.byref(pointer), ctypes.byref(length)):
        raise ValueError('Installer has no language metadata')
    languages = ctypes.cast(pointer, ctypes.POINTER(W.WORD))
    translations = [(languages[i], languages[i+1]) for i in range(0, length.value//2, 2)]
    for language, codepage in translations:
        key = f'\\StringFileInfo\\{language:04x}{codepage:04x}\\ProductName'
        if api.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)):
            if ctypes.wstring_at(pointer, length.value).rstrip('\0')=='Codexon QA':return
    raise ValueError('Only an installer built with -Isolated may run in QA')
