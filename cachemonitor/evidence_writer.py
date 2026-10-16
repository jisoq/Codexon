"""Bounded, non-blocking observation handoff; SQLite belongs to the writer thread."""
from queue import Queue, Empty, Full
import threading
import time
import sqlite3
from pathlib import Path
from .model_evidence import EvidenceStore


class _Preparation:
    def __init__(self):
        self.done=threading.Event()
        self.error=None


class EvidenceWriter:
    def __init__(self,path,capacity=1024,store_factory=EvidenceStore):
        self.path=Path(path)
        self.queue=Queue(maxsize=capacity)
        self.factory=store_factory
        self.lock=threading.Lock()
        self.pending_gap=None
        self.errors=0; self.dropped=0; self.written=0
        self.failure_streak=0
        self.preparing=0
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

    def prepare(self,timeout=30):
        """Prepare before admission; timeout leaves one owned, bounded queue item."""
        if self.closed:raise RuntimeError('Observation storage is closed')
        request=_Preparation();deadline=time.monotonic()+timeout
        with self.lock:self.preparing+=1
        try:self.queue.put(request,timeout=timeout)
        except Full:
            with self.lock:self.preparing-=1
            self.errors+=1;self.failure_streak+=1
            raise TimeoutError('Observation storage initialization did not start') from None
        if not request.done.wait(max(0,deadline-time.monotonic())):
            raise TimeoutError('Observation storage initialization did not complete')
        if request.error is not None:
            error=request.error;request.error=None
            raise error

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
                # New records wake Queue.get immediately. Healthy idle storage
                # has no polling duty; only an unsaved gap needs a retry clock.
                try:record=self.queue.get(timeout=1 if self.pending_gap else None)
                except Empty:
                    if store and self.pending_gap:
                        try:self.flush_gap(store)
                        except (sqlite3.Error,OSError):pass
                    continue
                if record is None:
                    self.queue.task_done()
                    continue
                if isinstance(record,_Preparation):
                    try:
                        if store is None:store=self.factory(self.path)
                    except (sqlite3.Error,OSError,ValueError) as error:
                        self.errors+=1;self.failure_streak+=1;record.error=error
                    finally:
                        with self.lock:self.preparing-=1
                        record.done.set();self.queue.task_done()
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
                    storage_written=self.written,storage_initializing=bool(self.preparing),
                    observation_dropped=self.dropped,storage_pending=self.queue.qsize(),
                    observation_gap_pending=self.pending_gap is not None)

    def close(self,timeout=5):
        if not self.closed:
            self.closed=True; self.stopping.set()
            try:self.queue.put_nowait(None)
            except Full:pass  # A full queue already keeps the draining writer awake.
        self.thread.join(timeout)
        return not self.thread.is_alive() and self.pending_gap is None
