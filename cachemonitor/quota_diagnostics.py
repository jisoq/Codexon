"""Bounded local diagnostics without account identities or RPC payloads."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import traceback


def record_failure(path, stage, error):
    logger = logging.getLogger('cachemonitor.quota.'+str(Path(path).resolve()))
    try:
        if not logger.handlers:
            target = Path(path).with_suffix('.diagnostics.log')
            target.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(target, maxBytes=256*1024, backupCount=2, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            logger.addHandler(handler)
            logger.setLevel(logging.WARNING)
            logger.propagate = False
        frames = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                  for f in traceback.extract_tb(error.__traceback__)[-8:]]
        logger.warning(json.dumps(dict(stage=stage, error=type(error).__name__,
            sqlite_code=getattr(error, 'sqlite_errorcode', None),
            sqlite_name=getattr(error, 'sqlite_errorname', None), frames=frames)))
    except OSError:
        pass  # Logging must never prevent recovery (e.g. a full disk).


def storage_issue(error):
    code = getattr(error, 'sqlite_errorcode', 0) or 0
    if code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return '한도 기록 저장 지연 · 자동 재시도 중'
    if code & 255 in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return '한도 기록 DB를 읽을 수 없습니다 · 기존 기록 보존 중'
    if code & 255 == sqlite3.SQLITE_FULL:
        return '한도 기록 저장 공간 부족 · 여유 공간 확보 필요'
    return '한도 기록 확인 지연 · 자동 재시도 중'
