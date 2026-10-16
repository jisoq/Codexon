"""Incremental builtin-sum arithmetic for the pinned CPython runtime."""
from math import isfinite
from struct import calcsize
import sys

from .prepared_store import RebuildRequired


_LONG_MAX = (1 << (8*calcsize('l')-1))-1
_LONG_MIN = -_LONG_MAX-1


class _Sum:
    """Incremental numeric builtin-sum semantics for the pinned CPython 3.12.

    Preserve the integer/float fast paths and compensation used by
    https://github.com/python/cpython/blob/v3.12.10/Python/bltinmodule.c .
    Category totals use sum(); cross-category weights deliberately use +=.
    """
    __slots__ = ('total', 'correction', 'phase')

    def __init__(self):
        self.total=0
        self.correction=0.0
        self.phase=0

    def add(self, value):
        kind=type(value)
        if kind not in (int,float,bool):
            raise RebuildRequired('category metrics require exact numeric scalars')
        if self.phase==0:
            if kind in (int,bool):
                if not (_LONG_MIN<=value<=_LONG_MAX and -sys.maxsize-1<=self.total+value<=sys.maxsize):
                    self.phase=2
                self.total+=value
            else:
                self.total+=value
                self.phase=1
            return
        if self.phase==1:
            if kind is float:
                combined=self.total+value
                self.correction+=((self.total-combined)+value if abs(self.total)>=abs(value)
                                  else (value-combined)+self.total)
                self.total=combined
                return
            if _LONG_MIN<=value<=_LONG_MAX:
                self.total+=float(value)
                return
            self.total=self.value()+value
            self.phase=2
            return
        self.total+=value

    def value(self):
        if self.phase==1 and self.correction and isfinite(self.correction):
            return self.total+self.correction
        return self.total
