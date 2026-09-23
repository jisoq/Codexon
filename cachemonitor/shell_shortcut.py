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
