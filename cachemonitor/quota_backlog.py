"""Bounded, replayable quota observations owned by one home's quota service."""
import hashlib
import json
from functools import wraps
from pathlib import Path
import sqlite3
import threading


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


class ObservationBacklog:
    # Keep distinct observations. At capacity, stop their producer instead of
    # deleting history. SQLite reuses freed pages; its rollback journal is also
    # bounded by the 16 MiB database ceiling. Only one failed write stays in RAM.
    MAX_ITEMS = 4096
    MAX_BYTES = 8 * 1024 * 1024
    MAX_ITEM_BYTES = 64 * 1024
    MAX_PAGES = 4096
    BATCH_SIZE = 32
    CONTROL_RESERVE = 32

    def __init__(self, ledger_path, home):
        path = Path(ledger_path)
        identity = hashlib.sha256(str(home).encode()).hexdigest()[:16]
        self.path = path.with_name(path.name + '.pending-' + identity + '.sqlite')
        self.db = None
        self.unsaved = None
        self.issue = ''
        self.rejected = ''
        self.lock = threading.RLock()

    def connection(self):
        if self.db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=.02, check_same_thread=False)
            try:
                db.execute('pragma page_size=4096')
                db.execute('pragma journal_mode=delete')
                db.execute('pragma synchronous=full')
                db.execute(f'pragma max_page_count={self.MAX_PAGES}')
                db.execute('create table if not exists pending('
                           'seq integer primary key autoincrement, kind text not null, data blob not null)')
                db.execute('create table if not exists budget(id integer primary key, items integer, bytes integer)')
                db.execute('insert or ignore into budget select 1,count(*),coalesce(sum(length(data)),0) from pending')
                db.execute('create trigger if not exists pending_added after insert on pending begin '
                           'update budget set items=items+1,bytes=bytes+length(new.data) where id=1; end')
                db.execute('create trigger if not exists pending_removed after delete on pending begin '
                           'update budget set items=items-1,bytes=bytes-length(old.data) where id=1; end')
                db.commit()
            except Exception:
                db.close()
                raise
            self.db = db
        return self.db

    def _store(self, kind, encoded):
        db = self.connection()
        try:
            db.execute('begin immediate')
            count, size = db.execute('select items,bytes from budget where id=1').fetchone()
            reserve = self.CONTROL_RESERVE if kind == 'control' else 0
            if count >= self.MAX_ITEMS + reserve or size + len(encoded) > self.MAX_BYTES + reserve*self.MAX_ITEM_BYTES:
                raise BufferError('quota observation backlog capacity')
            db.execute('insert into pending(kind,data) values(?,?)', (kind, encoded))
            db.commit()
        except Exception:
            db.rollback()
            raise

    @synchronized
    def flush(self):
        if self.unsaved is not None:
            try:
                self._store(*self.unsaved)
            except (sqlite3.Error, OSError, BufferError):
                self.issue = '한도 관측 임시 저장 실패: 새 조회를 중지했습니다. 종료 전 저장 공간을 확인하세요.'
                return False
            self.unsaved = None
        return True

    @synchronized
    def available(self):
        if self.rejected:
            self.issue = self.rejected
            return False
        if not self.flush():
            return False
        try:
            count, size = self.connection().execute(
                'select items,bytes from budget where id=1').fetchone()
            # Reserve room for one maximum-size response before making a request.
            if count >= self.MAX_ITEMS or size + self.MAX_ITEM_BYTES > self.MAX_BYTES:
                self.issue = '한도 관측 복구 대기 공간이 가득 찼습니다. 저장 복구까지 새 조회를 중지합니다.'
                return False
        except (sqlite3.Error, OSError):
            self.issue = '한도 관측 임시 저장소를 열 수 없습니다. 저장 복구까지 새 조회를 중지합니다.'
            return False
        self.issue = ''
        return True

    @synchronized
    def append(self, kind, value):
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        if len(encoded) > self.MAX_ITEM_BYTES:
            self.rejected = self.issue = '한도 관측 크기가 저장 한도를 초과했습니다. 새 조회를 중지했습니다. 앱을 다시 열어 재시도하세요.'
            return False
        if self.unsaved is not None:
            return False
        try:
            self._store(kind, encoded)
        except BufferError:
            self.issue = '한도 관측 복구 대기 공간이 가득 찼습니다. 새 기록을 받지 못했습니다.'
            return False
        except (sqlite3.Error, OSError):
            self.unsaved = (kind, encoded)
            self.issue = '한도 관측 임시 저장 실패: 새 조회를 중지했습니다. 종료 전 저장 공간을 확인하세요.'
        return True

    @synchronized
    def first(self):
        row = self.connection().execute('select seq,kind,data from pending order by seq limit 1').fetchone()
        if row is not None:
            return row[0], row[1], json.loads(row[2])
        if self.unsaved is not None:
            return None, self.unsaved[0], json.loads(self.unsaved[1])
        return None

    @synchronized
    def acknowledge(self, sequence):
        if sequence is None:
            self.unsaved = None
        else:
            with self.connection() as db:
                db.execute('delete from pending where seq=?', (sequence,))

    @synchronized
    def close(self):
        saved = self.flush()
        if self.db is not None:
            self.db.close()
            self.db = None
        return saved
