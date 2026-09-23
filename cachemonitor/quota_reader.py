"""Read recent reported quotas without building a usage index or parsing conversations."""
from datetime import datetime
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .quota import clean_limits


class QuotaReader:
    MAX_FILES = 16
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, home):
        self.home = Path(home).resolve()
        self.quota = None
        self.seen = {}
        self.bytes_read = 0

    def candidates(self):
        paths = set()
        for name in ('sessions', 'archived_sessions'):
            paths.update((self.home / name).rglob('*.jsonl'))
        # Older Codex databases can point to rollouts outside those two folders.
        database = self.home / 'state_5.sqlite'
        if database.is_file():
            try:
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                    for row in db.execute('select rollout_path from threads order by updated_at desc limit 32'):
                        if row[0]:
                            path = Path(row[0])
                            paths.add(path if path.is_absolute() else self.home / path)
            except sqlite3.Error:
                pass  # The standard session folders also work without a state database.
        found = []
        for path in paths:
            try:
                stat = path.stat()
                found.append((stat.st_mtime_ns, str(path), stat))
            except OSError:
                continue
        return sorted(found, reverse=True, key=lambda item: (item[0], item[1]))[:self.MAX_FILES]

    def poll(self):
        errors = []
        found = self.candidates()
        current_paths = {path for _, path, _ in found}
        self.seen = {path: value for path, value in self.seen.items() if path in current_paths}
        for _, path, stat in found:
            signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            previous = self.seen.get(path)
            if previous and previous[0] == signature:
                continue
            offset = 0
            if previous and previous[0][0] == stat.st_ino and stat.st_size > previous[0][1]:
                offset = previous[1]
            start = max(offset, stat.st_size - self.MAX_BYTES)
            try:
                with open(path, 'rb') as source:
                    source.seek(start)
                    if start > offset:
                        source.readline()  # Drop the incomplete leading JSONL record.
                    start = source.tell()
                    data = source.read(self.MAX_BYTES)
                self.bytes_read += len(data)
                last_newline = data.rfind(b'\n')
                complete = data[:last_newline + 1] if last_newline >= 0 else b''
                for line in complete.splitlines():
                    if b'"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                        payload = event.get('payload')
                        if event.get('type') != 'event_msg' or not isinstance(payload, dict) or payload.get('type') != 'token_count':
                            continue
                        limits = clean_limits(payload.get('rate_limits'))
                        observed = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                        if limits is not None and observed > 0 and (self.quota is None or observed >= self.quota['observed_at']):
                            self.quota = {**limits, 'observed_at': observed}
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                        continue
                self.seen[path] = (signature, start + len(complete))
            except OSError as exc:
                errors.append(f'한도 기록 읽기 실패: {type(exc).__name__}')
        return {'quota': self.quota, 'errors': sorted(set(errors)), 'files_checked': len(found),
                'bytes_read': self.bytes_read}
