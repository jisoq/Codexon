"""Bounded local diagnostics without account identities or RPC payloads."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import traceback
import time


def _logger(path):
    logger = logging.getLogger('cachemonitor.quota.'+str(Path(path).resolve()))
    if not logger.handlers:
        target = Path(path).with_suffix('.diagnostics.log')
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(target, maxBytes=256*1024, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        logger.propagate = False
    return logger


def record_slow_cycle(path, stages, source_age, last_report):
    """One bounded timing record per minute; never retain source records."""
    now=time.monotonic()
    if (sum(stages.values())<=10 and source_age<=30) or now-last_report<60:return last_report
    try:
        _logger(path).warning(json.dumps(dict(kind='slow_cycle',source_age_seconds=round(source_age,3),
            stage_seconds={key:round(value,3) for key,value in stages.items()})))
    except OSError:pass
    return now


def record_failure(path, stage, error):
    try:
        frames = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                  for f in traceback.extract_tb(error.__traceback__)[-8:]]
        timing={}
        method=getattr(error,'rpc_method',None)
        if method in ('initialize','account/read','account/rateLimits/read'):
            timing=dict(rpc_method=method,elapsed_seconds=round(error.elapsed_seconds,3))
        _logger(path).warning(json.dumps(dict(stage=stage, error=type(error).__name__,
            sqlite_code=getattr(error, 'sqlite_errorcode', None),
            sqlite_name=getattr(error, 'sqlite_errorname', None), frames=frames,**timing)))
    except OSError:
        pass  # Logging must never prevent recovery (e.g. a full disk).


def storage_issue(error):
    code = getattr(error, 'sqlite_errorcode', 0) or 0
    if code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return '한도 기록 저장 지연: 자동 재시도 중'
    if code & 255 in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return '한도 기록 DB를 읽을 수 없습니다. 기존 기록은 보존 중입니다.'
    if code & 255 == sqlite3.SQLITE_FULL:
        return '한도 기록 저장 공간 부족: 여유 공간 확보 필요'
    return '한도 기록 확인 지연: 자동 재시도 중'
