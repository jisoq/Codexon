"""Replay exact prepared speed recovery on an explicit SQLite copy.

The input opens read-only and is backed up to a new output directory. Read
settings match PreparedWriter. Classifier, selection and checkpoint capacities
use release defaults. Reports contain scalar counts and result digests.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import closing
from dataclasses import replace
import cProfile
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.benchmark_analysis import file_identity, memory
from tools.benchmark_overlay_latency import host_snapshot


def code_identity():
    paths = list((ROOT/'cachemonitor').glob('*.py'))
    paths.extend((ROOT/name for name in ('tools/benchmark_prepared_speed.py',
        'tools/benchmark_analysis.py', 'tools/benchmark_overlay_latency.py',
        'requirements-release.lock', 'cachemonitor/_cpu_native.pyd',
        'cachemonitor/_cpu_native.build.json')))
    hashes = {str(path.relative_to(ROOT)).replace('\\', '/'):hashlib.sha256(path.read_bytes()).hexdigest()
              for path in paths if path.is_file()}
    return dict(head=subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
        files=hashes, sha256=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest())


def result_digest(results):
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    for owner in sorted(results):
        for part in encoder.iterencode((owner, results[owner])):
            digest.update(part.encode('ascii'))
        digest.update(b'\n')
    return digest.hexdigest()


def retained_graph_bytes(*roots):
    pending = list(roots)
    seen = set()
    total = 0
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        total += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset, deque)):
            pending.extend(value)
        if type(value).__module__ == 'cachemonitor.speed_health':
            if hasattr(value, '__dict__'):
                pending.append(value.__dict__)
            for slot in getattr(type(value), '__slots__', ()):
                if hasattr(value, slot):
                    pending.append(getattr(value, slot))
    return total


def run(database, directory, *, profile=False, reference=False, checkpoint=False):
    from cachemonitor.prepared_store import Limits, PreparedStore, Snapshot, pack
    from cachemonitor.prepared_ingest import PreparedCohort, pages, preparation_signature
    from cachemonitor.prepared_speed_writer import PreparedSpeedWriter
    from cachemonitor.speed_health import SpeedHealth

    report = dict(completed=False, errors=[], stages=[], admissions=[], fallback=[],
                  host_before=host_snapshot(), memory_before=memory(), python=sys.version,
                  runtime=file_identity(sys.executable), backend=os.environ.get('CODEXON_CPU_BACKEND', 'auto'))
    report['identity_before'] = code_identity()
    report['input_before'] = file_identity(database)
    local = directory/'prepared.sqlite'
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as source, closing(sqlite3.connect(local)) as target:
        source.backup(target)
    profiler = cProfile.Profile() if profile else None
    original_stream = Snapshot.iter_speed_rows
    original_fallback = SpeedHealth._fallback
    original_admit = SpeedHealth._admit

    def stream(snapshot, ranges):
        try:
            yield from original_stream(snapshot, ranges)
        finally:
            report['admissions'].append(dict(snapshot.speed_work))

    def fallback(tracker, source, signature, reason):
        report['fallback'].append(dict(reason=reason, scanned_rows=source.read_rows))
        return original_fallback(tracker, source, signature, reason)

    def admit(tracker, proofs):
        try:
            return original_admit(tracker, proofs)
        except BaseException as error:
            trace = error.__traceback__
            while trace is not None and trace.tb_frame.f_code.co_name != '_admit':
                trace = trace.tb_next
            values = trace.tb_frame.f_locals if trace is not None else {}
            report['rejected_admission'] = {key:values.get(name) for key,name in (
                ('estimated_metadata_bytes', 'size'), ('sample_slots', 'samples'), ('memberships', 'pairs'))}
            raise

    def measure(label, call):
        started, cpu = time.perf_counter(), time.process_time()
        try:
            return call()
        finally:
            report['stages'].append(dict(name=label, seconds=time.perf_counter()-started,
                                        cpu_seconds=time.process_time()-cpu))

    Snapshot.iter_speed_rows = stream
    SpeedHealth._fallback = fallback
    SpeedHealth._admit = admit
    try:
        limits = replace(Limits(), read_seconds=60, read_vm_steps=20_000_000)
        with PreparedStore(local, limits) as store:
            def inventory():
                with store.read_current(seconds=60) as view:
                    owners = {entry['owner']:PreparedCohort._speed_stamp(entry) for entry in pages(view.catalog)
                              if entry['dependencies'].get('prepared') and not entry['metadata'].get('_internal_review')}
                    return owners, view.source_identity
            stamps, identity = measure('inventory', inventory)
            encoded = pack(identity, store.limits)
            report.update(generation=store.token.generation, owners=len(stamps),
                          canonical_rows=sum(stamp.length for stamp in stamps.values()))
            with closing(PreparedSpeedWriter(store, preparation=preparation_signature(),
                    cancel=threading.Event(), read_seconds=60)) as writer:
                try:
                    if profiler:
                        profiler.enable()
                    results = measure('current_results', lambda:writer.current_results(stamps, encoded))
                finally:
                    if profiler:
                        profiler.disable()
                report.update(metrics=dict(writer.tracker.metrics), read_rows=writer.last_read_rows,
                    checkpoint=writer.last_checkpoint, result_sha256=result_digest(results),
                    active=sum(bool(value.get('active')) for value in results.values()),
                    retained_graph_bytes=retained_graph_bytes(writer.tracker))
                if reference:
                    def classify_reference():
                        with writer.source.read_scope():
                            return writer.source.full_classify()
                    expected = measure('reference', classify_reference)
                    report['reference_sha256'] = result_digest(expected)
                    if report['reference_sha256'] != report['result_sha256']:
                        raise AssertionError('classification differs from the exact reference')
                    del expected
                if checkpoint:
                    def publish_checkpoint():
                        stage = store.begin(identity)
                        try:
                            writer.prepare(stage, stamps, stage.source, stamps)
                            token = writer.publish(stage, identity, ready=True)
                            return dict(generation=token.generation, writes=dict(stage.writes),
                                        checkpoint=dict(writer.last_checkpoint))
                        except BaseException:
                            writer.abort(stage)
                            raise
                    report['publication'] = measure('checkpoint', publish_checkpoint)
            if checkpoint:
                with closing(PreparedSpeedWriter(store, preparation=preparation_signature(),
                        cancel=threading.Event(), read_seconds=60)) as restarted:
                    results = measure('restart', lambda:restarted.current_results(stamps, encoded))
                    report['restart'] = dict(read_rows=restarted.last_read_rows,
                        checkpoint=restarted.last_checkpoint, result_sha256=result_digest(results),
                        retained_graph_bytes=retained_graph_bytes(restarted.tracker),
                        metrics=dict(restarted.tracker.metrics))
                    if restarted.last_read_rows or report['restart']['result_sha256'] != report['result_sha256']:
                        raise AssertionError('checkpoint restart did not preserve results without history')
        report['completed'] = True
    except BaseException as error:
        report['errors'].append(dict(type=type(error).__name__, message=str(error), trace=traceback.format_exc()))
    finally:
        Snapshot.iter_speed_rows = original_stream
        SpeedHealth._fallback = original_fallback
        SpeedHealth._admit = original_admit
        if profiler:
            profiler.dump_stats(str(directory/'classification.pstats'))
        report.update(host_after=host_snapshot(), memory_after=memory(), identity_after=code_identity(),
                      input_after=file_identity(database))
        if report['identity_before'] != report['identity_after'] or report['input_before'] != report['input_after']:
            report['errors'].append(dict(type='IdentityChanged', message='source or explicit input changed during the run'))
        report['failure_count'] = len(report['errors'])
        (directory/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(dict(report=str(directory/'report.json'), completed=report['completed'],
                              failures=report['failure_count'])), flush=True)
    return int(not report['completed'] or bool(report['errors']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True, help='Explicit prepared SQLite copy, read-only input')
    parser.add_argument('--output', type=Path, required=True, help='New isolated output directory')
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--reference', action='store_true')
    parser.add_argument('--checkpoint', action='store_true')
    args = parser.parse_args()
    database, output = args.database.resolve(), args.output.resolve()
    if os.name != 'nt' or not database.is_file() or output.exists():
        parser.error('Windows, an existing SQLite copy and a new output directory are required')
    output.mkdir(parents=True)
    return run(database, output, profile=args.profile, reference=args.reference, checkpoint=args.checkpoint)


if __name__ == '__main__':
    raise SystemExit(main())
