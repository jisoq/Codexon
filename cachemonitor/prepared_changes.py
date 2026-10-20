"""Bounded, contiguous in-process publication evidence."""
from collections import deque


class GenerationJournal:
    def __init__(self):
        self.entries=deque()
        self.bytes=0

    def add(self,value):
        from .analysis_engine import retained_bytes
        if value is None:return
        size=retained_bytes(value,4*1024*1024)
        if size>4*1024*1024 or len(value[2])+len(value[3])>128:
            self.entries.clear();self.bytes=0
            return
        while self.entries and (len(self.entries)>=16 or self.bytes+size>4*1024*1024):
            self.bytes-=self.entries.popleft()[1]
        self.entries.append((value,size));self.bytes+=size

    def values(self):return tuple(value for value,_ in self.entries)

    def between(self,base,target):
        if base is None:return None
        at=(base.instance,base.generation);end=(target.instance,target.generation)
        changed=set();removed=set()
        if at==end:return changed,removed
        for previous,current,owners,deleted in self.values():
            if previous is None or tuple(previous)!=at:continue
            changed.update(owners);removed.difference_update(owners)
            removed.update(deleted);changed.difference_update(deleted)
            at=tuple(current)
            if at==end:return changed,removed
        return None
