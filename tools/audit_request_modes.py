"""Inventory received mode fields without printing conversation content or writing sources."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cachemonitor.event_store import unpack_event
from cachemonitor.index import complete_thread_settings
from cachemonitor.model_evidence import default_path
from cachemonitor.pricing import request_tier
from cachemonitor.usage_paths import index_location


ABSENT = object()


def raw_value(value):
    if value is ABSENT:
        return 'absent', None
    if value is None:
        return 'null', None
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{0,256}', value):
        return 'string', value
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
    return type(value).__name__, 'sha256:' + sha256(encoded).hexdigest()


def classification(value, context):
    if context == 'response':
        return {'auto': 'response_auto', 'default': 'response_default',
                'priority': 'response_priority', '': 'response_unobserved'}.get(value, 'response_other') if isinstance(value, str) else 'response_other'
    if value is ABSENT:
        return 'Standard' if context == 'complete_snapshot' else 'historical_setting_required'
    if value is None:
        return 'Standard' if context in ('complete_snapshot', 'partial_settings', 'settings_clear') else 'unknown_null'
    if not isinstance(value, str):
        return 'invalid_type'
    resolved = request_tier({'service_tier': value})
    if resolved in ('Fast', 'Standard'):
        return resolved
    return 'unknown_empty' if value == '' else 'unsupported_value'


class Inventory:
    def __init__(self):
        self.counts = Counter()

    def add(self, channel, value, context=None, presence=None):
        kind, retained = raw_value(value)
        self.counts[(channel, kind, retained, presence,
                     classification(value, context or channel))] += 1

    def rows(self):
        return [dict(channel=channel, value_type=kind, value=value,
                     wire_key_present=presence, classification=classified, count=count)
                for (channel, kind, value, presence, classified), count
                in sorted(self.counts.items(), key=lambda item: repr(item[0]))]


def inspect_event(inventory, obj, prefix):
    if not isinstance(obj, dict):
        return
    payload = obj.get('payload')
    if not isinstance(payload, dict):
        return
    kind = obj.get('type')
    if kind in ('turn_context', 'token_usage_record'):
        inventory.add(prefix + kind, payload.get('service_tier', ABSENT), kind)
    elif kind == 'event_msg' and payload.get('type') == 'thread_settings_applied':
        settings = payload.get('thread_settings')
        if isinstance(settings, dict):
            context = 'complete_snapshot' if complete_thread_settings(settings) else 'partial_settings'
            channel = 'thread_settings' if prefix == 'indexed_' else context
            inventory.add(prefix + channel, settings.get('service_tier', ABSENT), context)
        else:
            inventory.add(prefix + 'invalid_settings', settings, 'invalid_settings')


def connect(path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    return connection


def audit(index, evidence, ledger):
    inventory = Inventory()
    report = dict(format=1, observed_at=datetime.now(timezone.utc).isoformat(),
                  source_access='read_only', scopes={})
    with closing(connect(index)) as db:
        count = 0
        for value, in db.execute('SELECT data FROM event_records'):
            inspect_event(inventory, json.loads(unpack_event(value)), 'indexed_')
            count += 1
        report['scopes']['indexed_event_rows'] = count
        archive = 0
        for value, in db.execute('SELECT data FROM usage_archive'):
            record = json.loads(value)
            inventory.add('usage_archive', record.get('service_tier', ABSENT), 'usage_record')
            archive += 1
        report['scopes']['usage_archive_rows'] = archive
        paths = [Path(row[0]) for row in db.execute('SELECT path FROM files')]
    failures = Counter()
    files = lines = 0
    for path in paths:
        try:
            with path.open('rb') as stream:
                for line in stream:
                    lines += 1
                    if not any(marker in line for marker in (
                            b'turn_context', b'token_usage_record', b'thread_settings_applied')):
                        continue
                    try:
                        inspect_event(inventory, json.loads(line), 'raw_')
                    except (ValueError, UnicodeError):
                        failures['invalid_json_rows'] += 1
            files += 1
        except OSError as error:
            failures[type(error).__name__] += 1
    report['scopes']['raw_rollouts'] = dict(indexed_files=len(paths), accessible_files=files,
                                            scanned_lines=lines, failures=dict(failures))
    with closing(connect(evidence)) as db:
        total = db.execute('SELECT count(*) FROM model_observations').fetchone()[0]
        attempts = 0
        for requested, responded, present in db.execute(
                'SELECT requested_service_tier,response_service_tier,wire_service_tier_present '
                'FROM model_observations WHERE seq IN '
                '(SELECT max(seq) FROM model_observations GROUP BY home,attempt)'):
            inventory.add('wire_request', ABSENT if present == 0 else requested,
                          'wire_request', present)
            inventory.add('wire_response', responded, 'response')
            attempts += 1
        report['scopes']['wire'] = dict(stored_observation_rows=total, latest_attempt_rows=attempts)
    with closing(connect(ledger)) as db:
        count = 0
        for value, in db.execute('SELECT data FROM mode_events'):
            record = json.loads(value)
            action = record.get('action')
            inventory.add('settings_log_' + str(action),
                          record.get('configured_service_tier') if action != 'unchanged' else ABSENT,
                          'settings_clear' if action == 'clear' else 'settings_log')
            count += 1
        report['scopes']['saved_settings_log_rows'] = count
    report['values'] = inventory.rows()
    report['limitations'] = [
        'Each source database uses its own read-only snapshot; live sources may advance between snapshots.',
        'Indexed applied snapshots are sanitized; only raw rollouts preserve original field presence.',
        'Missing rollout files cannot be reconstructed from the retained index.',
        'Wire presence null means older observations did not preserve original key presence.',
        'Wire strings do not distinguish original JSON null from other sanitized empty values.',
        'Response tiers never determine the requested subscription mode.',
        'Invalid field payloads are counted by type and digest without exposing their contents.',
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, default=index_location(None))
    parser.add_argument('--evidence', type=Path, default=default_path())
    parser.add_argument('--ledger', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.index, args.evidence,
                   args.ledger or args.index.with_name('quota-cycles.sqlite'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(output=str(args.output.resolve()), scopes=report['scopes']), ensure_ascii=True))


if __name__ == '__main__':
    main()
