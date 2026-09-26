"""Collector process ownership and cooperative retirement of old IPC files."""
import json
import os
from pathlib import Path
import sqlite3
import time
import zlib

from .proxy_target import option
from . import proxy_identity as identity


def collector_process(channel,snapshot):
    """Match the kernel identity and argv before controlling one collector."""
    source=snapshot.get('collection') or {}
    process=identity.process_identity(source['pid'])
    if not process:raise RuntimeError('수집기의 실행 정보가 변경되었습니다.')
    command=identity.process_command(process['pid'])
    homes=[str(Path(command[n+1]).resolve()) for n,arg in enumerate(command[:-1]) if arg=='--codex-home']
    evidence=option(command,'--evidence-path',str(channel.default_evidence))
    created=source.get('process_created')
    executable=source.get('executable')
    if (not command or '--usage-collector' not in command or not homes or
        not set(homes).issubset(set(channel.homes)) or
        not set(snapshot.get('homes',[])).issubset(set(channel.homes)) or
        Path(option(command,'--index-path','')).resolve()!=channel.path or
        os.path.normcase(str(Path(evidence).resolve()))!=channel.scope or
        (created is not None and process['created']!=created) or
        (executable and not identity.executable_matches(process['executable'],executable)) or
        not identity.executable_matches(process['executable'],command[0]) or not identity.same_process(process)):
        raise RuntimeError('수집기의 프로세스 소유권을 확인하지 못했습니다.')
    return process


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
        process=collector_process(channel,snapshot)
        # A version change must never kill a source scan or a record commit.
        db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',(source['instance'],'stop'));db.commit()
    deadline=clock()+30
    while not identity.process_exited(process) or locked(lock):
        if clock()>=deadline:raise RuntimeError('이전 수집기의 기록 저장과 종료 대기')
        sleep(.2)
    return True
