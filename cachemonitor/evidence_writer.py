"""Bounded, non-blocking observation handoff; SQLite belongs to the writer thread."""
from queue import Queue, Empty, Full
import threading
import time
import sqlite3
from pathlib import Path
from .model_evidence import EvidenceStore


class EvidenceWriter:
    def __init__(self,path,capacity=1024,store_factory=EvidenceStore):
        self.path=Path(path)
        self.queue=Queue(maxsize=capacity)
        self.factory=store_factory
        self.lock=threading.Lock()
        self.pending_gap=None
        self.errors=0; self.dropped=0; self.written=0
        self.failure_streak=0
        self.closed=False
        self.stopping=threading.Event()
        self.thread=threading.Thread(target=self.run,name='observation-storage',daemon=True)
        self.thread.start()

    def lost(self,record):
        with self.lock:
            start=record.get('ts',time.time()); end=time.time()
            if self.pending_gap:
                start=min(start,self.pending_gap[0]); end=max(end,self.pending_gap[1])
            self.pending_gap=(start,end)
            self.dropped+=1

    def write(self,home,**record):
        copy={'home':str(home),**record}
        if self.closed:
            self.lost(copy); return False
        try:self.queue.put_nowait(copy)
        except Full:
            self.lost(copy); return False
        return True

    def flush_gap(self,store):
        with self.lock:
            gap=self.pending_gap
        if gap:
            store.gap(*gap)
            with self.lock:
                if self.pending_gap==gap:self.pending_gap=None

    def run(self):
        store=None
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:record=self.queue.get(timeout=.05)
                except Empty:
                    if store and self.pending_gap:
                        try:self.flush_gap(store)
                        except (sqlite3.Error,OSError):pass
                    continue
                try:
                    if store is None:store=self.factory(self.path)
                    self.flush_gap(store)
                    store.write(**record)
                    self.written+=1; self.failure_streak=0
                except (sqlite3.Error,OSError,ValueError):
                    self.errors+=1; self.failure_streak+=1; self.lost(record)
                finally:self.queue.task_done()
            if store:self.flush_gap(store)
        except (sqlite3.Error,OSError):
            self.errors+=1
        finally:
            if store:store.close()

    def status(self):
        return dict(storage_errors=self.errors,storage_failure_streak=self.failure_streak,
                    observation_dropped=self.dropped,storage_pending=self.queue.qsize(),
                    observation_gap_pending=self.pending_gap is not None)

    def close(self,timeout=5):
        self.closed=True; self.stopping.set(); self.thread.join(timeout)
        return not self.thread.is_alive() and self.pending_gap is None
