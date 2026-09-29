"""Persist the recovery shortcut's Windows application identity without Qt."""
import ctypes as C
from ctypes import wintypes as W
import uuid


class GUID(C.Structure):
    _fields_=[('data',C.c_byte*16)]
    @classmethod
    def parse(cls,value):return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class PROPERTYKEY(C.Structure):
    _fields_=[('fmtid',GUID),('pid',W.DWORD)]


class Value(C.Union):
    _fields_=[('text',W.LPWSTR),('storage',C.c_byte*16)]


class PROPVARIANT(C.Structure):
    _fields_=[('vt',W.WORD),('r1',W.WORD),('r2',W.WORD),('r3',W.WORD),('value',Value)]


def shortcut(path,target=None,cwd=None):
    """Save or read a link through IShellLinkW and IPersistFile (Unicode)."""
    ole=C.OleDLL('ole32');initialized=False
    ole.CoInitializeEx.argtypes=[C.c_void_p,W.DWORD]
    try:
        ole.CoInitializeEx(None,2);initialized=True
    except OSError as exc:
        if getattr(exc,'winerror',None)!=-2147417850:raise
    link=C.c_void_p();persist=C.c_void_p()
    def invoke(pointer,index,*args):
        table=C.cast(pointer,C.POINTER(C.POINTER(C.c_void_p))).contents
        result=C.WINFUNCTYPE(C.c_long,C.c_void_p,*[type(arg) for arg in args])(table[index])(pointer,*args)
        if result<0:raise OSError(f'Shortcut method {index} failed: {result:#x}')
    try:
        clsid=GUID.parse('00021401-0000-0000-c000-000000000046')
        iid=GUID.parse('000214f9-0000-0000-c000-000000000046')
        ole.CoCreateInstance.argtypes=[C.POINTER(GUID),C.c_void_p,W.DWORD,C.POINTER(GUID),C.POINTER(C.c_void_p)]
        ole.CoCreateInstance(C.byref(clsid),None,1,C.byref(iid),C.byref(link))
        file_iid=GUID.parse('0000010b-0000-0000-c000-000000000046')
        invoke(link,0,C.pointer(file_iid),C.pointer(persist))
        if target is not None:
            invoke(link,20,W.LPCWSTR(str(target)))
            invoke(link,9,W.LPCWSTR(str(cwd)))
            invoke(persist,6,W.LPCWSTR(str(path)),W.BOOL(True))
        else:
            invoke(persist,5,W.LPCWSTR(str(path)),W.DWORD(0))
        target_buffer=C.create_unicode_buffer(32768);cwd_buffer=C.create_unicode_buffer(32768)
        invoke(link,3,C.cast(target_buffer,W.LPWSTR),C.c_int(len(target_buffer)),C.c_void_p(),W.DWORD(0))
        invoke(link,8,C.cast(cwd_buffer,W.LPWSTR),C.c_int(len(cwd_buffer)))
        return target_buffer.value,cwd_buffer.value
    finally:
        for pointer in (persist,link):
            if pointer:
                table=C.cast(pointer,C.POINTER(C.POINTER(C.c_void_p))).contents
                C.WINFUNCTYPE(W.ULONG,C.c_void_p)(table[2])(pointer)
        if initialized:ole.CoUninitialize()


def application_id(path,value=None):
    ole=C.OleDLL('ole32');shell=C.OleDLL('shell32')
    ole.CoInitializeEx.argtypes=[C.c_void_p,W.DWORD]
    initialized=False
    try:
        ole.CoInitializeEx(None,2);initialized=True
    except OSError as exc:
        if getattr(exc,'winerror',None)!=-2147417850:raise
    pointer=C.c_void_p()
    iid=GUID.parse('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99')
    key=PROPERTYKEY(GUID.parse('9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3'),5)
    shell.SHGetPropertyStoreFromParsingName.argtypes=[W.LPCWSTR,C.c_void_p,W.DWORD,C.POINTER(GUID),C.POINTER(C.c_void_p)]
    def invoke(index,*args):
        table=C.cast(pointer,C.POINTER(C.POINTER(C.c_void_p))).contents
        types=[type(arg) for arg in args]
        result=C.WINFUNCTYPE(C.c_long,C.c_void_p,*types)(table[index])(pointer,*args)
        if result<0:raise OSError(f'Shortcut property operation failed: {result:#x}')
    try:
        shell.SHGetPropertyStoreFromParsingName(str(path),None,2 if value is not None else 0,C.byref(iid),C.byref(pointer))
        variant=PROPVARIANT()
        if value is not None:
            buffer=C.create_unicode_buffer(value);variant.vt=31;variant.value.text=C.cast(buffer,W.LPWSTR)
            invoke(6,C.pointer(key),C.pointer(variant));invoke(7)
            return value
        invoke(5,C.pointer(key),C.pointer(variant))
        try:return variant.value.text if variant.vt==31 else None
        finally:
            ole.PropVariantClear.argtypes=[C.POINTER(PROPVARIANT)];ole.PropVariantClear(C.byref(variant))
    finally:
        if pointer:
            table=C.cast(pointer,C.POINTER(C.POINTER(C.c_void_p))).contents
            C.WINFUNCTYPE(W.ULONG,C.c_void_p)(table[2])(pointer)
        if initialized:ole.CoUninitialize()
