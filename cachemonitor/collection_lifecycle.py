"""Cooperative collector upgrades and preservation of the previous default index."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time
import zlib
import tempfile

from .proxy_target import option
from . import proxy_identity as identity
from .usage_paths import index_location,legacy_index_location


def startup_snapshot(channel):
    """Recognize the old default-path bug for control only, never for display."""
    from .usage_collection import CollectionScopeError
    try:return channel.read_header(),False
    except CollectionScopeError:
        if channel.index_path is not None or channel.path!=index_location():raise
        with closing(sqlite3.connect(channel.snapshot_path.as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute('SELECT scope,payload FROM snapshot WHERE id=1').fetchone()
        if not row or row[0]!=channel.scope:raise
        snapshot=json.loads(zlib.decompress(row[1]));source=snapshot.get('collection') or {}
        version=source.get('version','').split('.')
        if (len(version)!=4 or not all(p.isdigit() for p in version) or
                not (tuple(map(int,version))<=(2026,9,27,1)) or
                snapshot.get('collection_schema',1) not in (1,2) or
                Path(snapshot.get('index',{}).get('path','')).resolve()!=legacy_index_location() or
                not snapshot.get('homes') or not set(snapshot['homes']).issubset(channel.homes) or
                not source.get('instance') or not source.get('pid') or not source.get('executable')):raise
        from .usage_collection import locked
        lock=channel.companion('.collector.lock')
        if not locked(lock):return snapshot,True
        try:
            process=identity.process_identity(source['pid'])
            if process:
                command=identity.process_command(process['pid'])
                # The remaining identity checks are also required by AppServices.
                if '--default-index' not in command or not identity.same_process(process):raise
        except (OSError,RuntimeError):
            if not locked(lock):return snapshot,True
            raise
        return snapshot,True


def migrate_default_index(channel):
    """Called only by CollectorService while holding its exclusive writer lock."""
    from .usage_collection import locked
    destination=channel.path;source=legacy_index_location()
    if channel.index_path is not None or destination.exists() or not source.is_file():return False
    # Earlier collectors may use either lock spelling. Never copy their index
    # until the owning process has completed its cooperative shutdown.
    for lock in (channel.path.with_suffix('.collector.lock'),
                 source.with_suffix('.collector.lock'),
                 source.with_name(source.name+'.codexon-collector.lock')):
        if lock.exists() and locked(lock):
            raise RuntimeError('이전 수집기의 기록 저장과 종료 대기')
    descriptor,name=tempfile.mkstemp(prefix=destination.name+'.migration-',suffix='.tmp',dir=destination.parent)
    os.close(descriptor);temporary=Path(name)
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as old:
            tables={row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'files','events','metadata'}.issubset(tables) or tables-{'files','events','metadata','usage_archive'}:
                raise RuntimeError('이전 사용량 색인의 형식을 확인하지 못했습니다. 원본을 보존합니다.')
            with closing(sqlite3.connect(temporary)) as new:
                old.backup(new)
                if new.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
                    raise RuntimeError('이전 사용량 색인을 검증하지 못했습니다. 원본을 보존합니다.')
                new.execute('PRAGMA journal_mode=DELETE')
        # Atomic publication without overwriting an index created by another
        # owner. Both names are on the same filesystem; unlink only our temp.
        try:os.link(temporary,destination)
        except FileExistsError:return False
        return True
    finally:temporary.unlink(missing_ok=True)


def retire_legacy(channel,*,clock=time.monotonic,sleep=time.sleep):
    # This is control-plane migration only. Source collection and new snapshots
    # always use CollectionChannel's sole current IPC path.
    from .usage_collection import locked
    path=channel.path.with_suffix('.collection.sqlite')
    lock=channel.path.with_suffix('.collector.lock')
    if not path.exists() or not locked(lock):return False
    with sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=5) as db:
        row=db.execute('SELECT scope,payload FROM snapshot WHERE id=1').fetchone()
        if not row:raise RuntimeError('이전 수집기의 실행 정보 확인 대기')
        snapshot=json.loads(zlib.decompress(row[1]));source=snapshot.get('collection') or {}
        if (os.path.normcase(str(Path(row[0]).resolve()))!=channel.scope or
            Path(snapshot.get('index',{}).get('path','')).resolve()!=channel.path or
            not set(snapshot.get('homes',[])).issubset(set(channel.homes))):
            raise RuntimeError('다른 색인 또는 Codex 홈의 이전 수집기를 보존합니다.')
        process=identity.process_identity(source['pid'])
        if not process:raise RuntimeError('이전 수집기의 실행 정보가 변경되었습니다.')
        command=identity.process_command(process['pid'])
        homes=[str(Path(command[n+1]).resolve()) for n,arg in enumerate(command[:-1]) if arg=='--codex-home']
        evidence=option(command,'--evidence-path',str(channel.default_evidence))
        if ('--usage-collector' not in command or not homes or not set(homes).issubset(set(channel.homes)) or
            Path(option(command,'--index-path','')).resolve()!=channel.path or
            os.path.normcase(str(Path(evidence).resolve()))!=channel.scope or
            Path(command[0]).resolve()!=Path(process['executable']).resolve() or not identity.same_process(process)):
            raise RuntimeError('이전 수집기의 프로세스 소유권을 확인하지 못했습니다.')
        # A version change must never kill a source scan or a record commit.
        db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',(source['instance'],'stop'));db.commit()
    deadline=clock()+30
    while identity.same_process(process) or locked(lock):
        if clock()>=deadline:raise RuntimeError('이전 수집기의 기록 저장과 종료 대기')
        sleep(.2)
    return True
