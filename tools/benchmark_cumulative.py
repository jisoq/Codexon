from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import cProfile
from dataclasses import asdict, dataclass, replace
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import pstats
import shutil
import statistics
import subprocess
import sys
from threading import Event, Thread
import time
import traceback


NOW = 1793185200.0
DAY = 86400
START = NOW - 7 * DAY
MODELS = ('gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna')


def canonical(value):
    if isinstance(value, dict):
        return ['dict', [[str(key), canonical(item)] for key, item in sorted(value.items())]]
    if isinstance(value, (list, tuple)):
        return [type(value).__name__, [canonical(item) for item in value]]
    if isinstance(value, float):
        return ['float', value.hex()]
    return [type(value).__name__, value]


def digest(value):
    packet = json.dumps(canonical(value), ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(packet).hexdigest()


def file_identity(path):
    path = Path(path).resolve()
    return dict(path=str(path), bytes=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


@dataclass
class Owner:
    key: tuple[str, str]
    ordinal: int
    metadata: dict
    rows: list[dict]


def fixture(calls, owners):
    population = []
    for number in range(owners):
        count = calls // owners + (number < calls % owners)
        home, sid = f'synthetic-home-{number % 3}', f'session-{number:03d}'
        metadata = dict(home=home, id=sid, sid=sid, title=f'Session {number}',
                        cwd=f'D:/synthetic/project-{number % 12:02d}',
                        project=f'project-{number % 12:02d}',
                        project_name=f'Project {number % 12}', source='CLI', _internal_review=False)
        rows = []
        for index in range(count):
            input_tokens = 1000 + ((index * 7919 + number * 31) % 190000)
            if index == 1 and number == 0:
                input_tokens = 2 ** 53 + 1
            output = 0 if index % 17 == 0 else 10 + (index * 43 + number) % 4000
            latency = 200 + (index * 37 + number) % 18000
            row = dict(home=home, sid=sid, key=f'{sid}-{index:06d}', turn=f'turn-{index // 4}',
                       session_ordinal=index + 1,
                       ts=START + (index // 2 * 2) * ((7 * DAY - 600) / max(1, count)) + number,
                       model='미확인' if index % 37 == 0 else MODELS[index % len(MODELS)],
                       effort='high' if index % 2 else 'low',
                       service_tier='Fast' if index % 3 == 0 else 'Standard',
                       input=input_tokens, cached=input_tokens * (index % 9) // 10,
                       output=output, reasoning=None if index % 23 == 0 else output // 3,
                       cost=None if index % 41 == 0 else .001 * (index % 97),
                       duration=None if index % 31 == 0 else latency / 1000,
                       completion_latency_ms=latency, output_speed=output * 1000 / latency,
                       response_status='completed', timing_valid=index % 19 != 0,
                       observation_missing=index % 29 == 0,
                       input_conflict=index % 101 == 0, output_conflict=False,
                       model_conflict=False, mode_conflict=False,
                       transport='websocket', transport_source='response_id')
            rows.append(row)
        population.append(Owner((home, sid), number, metadata, rows))
    return population


def dependencies(owner):
    return dict(source_digest=digest(owner.rows), pricing=1, mode=1, ownership=1,
                classifier=1, preparation=1, call_count=len(owner.rows),
                coverage_bounds=(min(row['ts'] for row in owner.rows), max(row['ts'] for row in owner.rows)))


def stage_owner(stage, owner, rows, *, reconcile=False, checkpoint=None):
    stage.owner(*owner.key, owner.metadata, dependencies(owner), source_ordinal=owner.ordinal,
                checkpoint=checkpoint)
    stage.calls(owner.key, rows, reconcile=reconcile)


def seed(path, population):
    from cachemonitor.prepared_store import PreparedStore
    with PreparedStore(path) as store:
        source = ('synthetic-cumulative', 1)
        stage = store.begin(source)
        for owner in population:
            stage_owner(stage, owner, owner.rows, reconcile=True)
        token = stage.publish(source, ready=True)
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return dict(token=(token.instance, token.generation), disk=store.disk_usage(), writes=dict(stage.writes))


def token_tuple(token):
    return token.instance, token.generation


def publish_change(store, owner, kind, revision):
    from cachemonitor.comparison_usage import dependency_stamp
    before = store.token
    prior = dependencies(owner)
    source = ('synthetic-cumulative', revision)
    deleted = []
    if kind == 'append':
        last = len(owner.rows)
        tail = [dict(owner.rows[-1], key=f'{owner.key[1]}-append-{index}',
                     session_ordinal=last + index + 1, ts=NOW - 120 + index)
                for index in range(32)]
        owner.rows.extend(tail)
        checkpoint = dict(comparison_append=dict(format=1, prior=dependency_stamp(prior),
                           target=dependency_stamp(dependencies(owner)), ordinal=last))
        written = tail
    else:
        index = len(owner.rows) // 2
        owner.rows[index] = dict(owner.rows[index], input=12345, cached=0, cost=.234,
                                output=1999, reasoning=None, completion_latency_ms=4321,
                                output_speed=1999 * 1000 / 4321)
        written = [owner.rows[index]]
        deleted = [owner.rows.pop(index - 1)['key']]
        checkpoint = None
    wall, cpu = time.perf_counter(), time.process_time()
    stage = store.begin(source)
    stage_owner(stage, owner, written, checkpoint=checkpoint)
    if deleted:
        stage.delete('calls', owner.key, deleted)
    token = stage.publish(source, ready=True)
    return dict(kind=kind, elapsed_ms=(time.perf_counter() - wall) * 1000,
                cpu_ms=(time.process_time() - cpu) * 1000, writes=dict(stage.writes),
                changed_rows=len(written), deleted_rows=len(deleted),
                transition=(token_tuple(before), token_tuple(token), [owner.key], []))


def query_shapes(width):
    comparison = dict(page=1, category_usage=True, presentation=True, include_whole_history=False,
                      start=START, end=NOW, now=NOW, period='all',
                      usage=dict(comparison=True, dimension='project', metric='calls', showUnknown=True))
    performance = dict(page=5, presentation=True, start=START, end=NOW, now=NOW, period='custom',
                       plot_width=width, granularity='auto', trend_period='7d', time_range=[START, NOW],
                       trend_selection=dict(models=None, modes=['Standard', 'Fast']))
    timeline = dict(performance, trend_mode='timeline')
    clock = dict(performance, trend_mode='hour_of_day')
    selected = dict(models=[MODELS[0]], modes=['Fast'])
    cases = [
        ('comparison-cold', comparison, None),
        ('comparison-warm', comparison, 'comparison-cold'),
        ('comparison-latency', dict(comparison, usage=dict(comparison['usage'], metric='latency')), None),
        ('comparison-input-band', dict(comparison, usage=dict(comparison['usage'], dimension='input_band')), None),
        ('comparison-return', comparison, 'comparison-cold'),
        ('timeline-cold', timeline, None), ('clock-cold', clock, None),
        ('timeline-warm', timeline, 'timeline-cold'), ('clock-warm', clock, 'clock-cold'),
        ('timeline-selected', dict(timeline, trend_selection=selected), None),
        ('clock-selected', dict(clock, trend_selection=selected), None),
        ('timeline-three-days', dict(timeline, time_range=[NOW - 3 * DAY, NOW]), None),
        ('clock-three-days', dict(clock, time_range=[NOW - 3 * DAY, NOW]), None),
        ('timeline-return', timeline, 'timeline-cold'), ('clock-return', clock, 'clock-cold'),
    ]
    return cases, comparison, timeline, clock


def expected_calls(population, query):
    start, end = query.get('time_range') or (query['start'], query['end'])
    selection = query.get('trend_selection', {})
    performance = query['page'] == 5
    count = 0
    for owner in population:
        for row in owner.rows:
            if not start <= row['ts'] <= end:
                continue
            if performance:
                if row['model'] in ('미확인', 'codex-auto-review', None, ''):
                    continue
                if selection.get('models') is not None and row['model'] not in selection['models']:
                    continue
                if selection.get('modes') is not None and row['service_tier'] not in selection['modes']:
                    continue
            count += 1
    return count


def run_query(handler, token, label, query, changes, population, repetition, profile=None, progress=None):
    if handler.engine is not None:
        handler.engine.read_metrics.clear()
    request = dict(kind='query', id=label, logical=label, query=deepcopy(query), _prepared_changes=changes)
    if progress is not None:
        request['_progress'] = progress
    wall, cpu = time.perf_counter(), time.process_time()
    if profile is not None:
        profile.enable()
    try:
        response = handler(request, token, lambda: False)
    finally:
        if profile is not None:
            profile.disable()
    elapsed, cpu_ms = (time.perf_counter() - wall) * 1000, (time.process_time() - cpu) * 1000
    if response['kind'] != 'result':
        raise AssertionError(f'{label} returned {response["kind"]}')
    if tuple(response['prepared_token']) != token_tuple(token):
        raise AssertionError(f'{label} returned another prepared generation')
    value = response['result']['performance' if query['page'] == 5 else 'usage']
    calls = value['calls'] if query['page'] == 5 else value['metrics']['calls']['value']
    expected = expected_calls(population, query)
    if calls != expected:
        raise AssertionError(f'{label} returned {calls} calls, expected {expected}')
    if query.get('trend_mode') == 'hour_of_day':
        panel = next(panel for panel in value['panels'] if panel['key'] == 'count')
        plotted = sum(point['calls'] for line in panel['lines'] for point in line['points'])
        if plotted != calls:
            raise AssertionError(f'{label} plotted {plotted} calls, expected {calls}')
    return dict(label=label, repetition=repetition, elapsed_ms=elapsed, cpu_ms=cpu_ms,
                calls=calls, expected_calls=expected, sha256=digest(value),
                work=dict(handler.engine.read_metrics), metrics=response['metrics'],
                generation=token.generation, query=query), value


def checkpoint(directory, label, repetition, result=None):
    entry = dict(label=label, repetition=repetition, state='complete' if result else 'start')
    if result:
        entry.update(elapsed_ms=result['elapsed_ms'], calls=result['calls'], sha256=result['sha256'],
                     work=result['work'])
    with (directory / 'progress.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
    print(json.dumps({key: value for key, value in entry.items() if key != 'work'}), flush=True)


def run_suite(database, population, repetition, width, profile, report, payloads):
    from cachemonitor.prepared_query import PreparedQuery
    from cachemonitor.prepared_store import PreparedStore
    cases, comparison, timeline, clock = query_shapes(width)
    hashes = {}
    with PreparedStore(database) as store:
        handler = PreparedQuery(database)
        try:
            for label, query, origin in cases:
                report['phase'] = label
                checkpoint(database.parent, label, repetition)
                entry, value = run_query(handler, store.token, label, query, [], population, repetition, profile)
                report['runs'].append(entry)
                checkpoint(database.parent, label, repetition, entry)
                if origin and entry['sha256'] != hashes[origin]:
                    raise AssertionError(f'{label} changed the result of {origin}')
                hashes[label] = entry['sha256']
                if repetition == 0:
                    payloads[label] = canonical(value)
            for revision, kind in enumerate(('append', 'correction-delete'), 2):
                report['phase'] = kind + '-publication'
                change = publish_change(store, population[0 if kind == 'append' else 1], kind, revision)
                report['publications'].append(dict(repetition=repetition, **change))
                changes = [change['transition']]
                for prefix, query in (('comparison', comparison), ('timeline', timeline), ('clock', clock)):
                    label = prefix + '-' + kind
                    report['phase'] = label
                    checkpoint(database.parent, label, repetition)
                    entry, value = run_query(handler, store.token, label, query, changes, population, repetition, profile)
                    report['runs'].append(entry)
                    checkpoint(database.parent, label, repetition, entry)
                    if repetition == 0:
                        payloads[label] = canonical(value)
                        oracle = PreparedQuery(database)
                        try:
                            proof, _ = run_query(oracle, store.token, label + '-fresh-proof', query,
                                                 [], population, repetition)
                        finally:
                            oracle.close()
                        if proof['sha256'] != entry['sha256']:
                            raise AssertionError(f'{label} differs from a fresh complete query')
                        report['proofs'].append(dict(label=label, sha256=proof['sha256'],
                                                     calls=proof['calls'], work=proof['work']))
        finally:
            handler.close()


def wal_probe(database, population, width):
    from cachemonitor.prepared_query import PreparedQuery
    from cachemonitor.prepared_store import PreparedStore, WriteDeferred
    _, comparison, _, _ = query_shapes(width)
    entered, release, finished = Event(), Event(), Event()
    result = {}
    deadline = time.monotonic() + 60

    def progress(value):
        if isinstance(value, dict) and value.get('stage') == 'aggregate' and not entered.is_set():
            result['reader_progress'] = value
            entered.set()
            if not release.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError('WAL probe reader release deadline exceeded')

    def read(token):
        handler = PreparedQuery(database)
        try:
            result['reader'], _ = run_query(handler, token, 'wal-reader', comparison,
                                            [], population, 0, progress=progress)
        except BaseException:
            result['reader_error'] = traceback.format_exc()
        finally:
            handler.close()
            finished.set()

    with PreparedStore(database) as store:
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        original = store.token
        thread = Thread(target=read, args=(original,), name='synthetic-wal-reader')
        thread.start()
        try:
            if not entered.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError('WAL probe reader never reached aggregate progress')
            changed = deepcopy(population[0])
            result['publication'] = publish_change(store, changed, 'correction-delete', 100)
            pending = store.begin(('synthetic-cumulative', 101))
            wal = store.disk_usage()['wal']
            result['default_limits'] = asdict(store.limits)
            store.limits = replace(store.limits, wal_bytes=wal + 4096, write_reserve_bytes=8192)
            result['scaled_limits'] = asdict(store.limits)
            wall = time.perf_counter()
            try:
                stage_owner(pending, changed, [], checkpoint=None)
            except WriteDeferred as error:
                result['deferred'] = dict(type=type(error).__name__, message=str(error),
                                          elapsed_ms=(time.perf_counter() - wall) * 1000,
                                          wal_before=wal, wal_after=store.disk_usage()['wal'])
            else:
                raise AssertionError('WAL writer did not defer while the active reader pinned its pages')
            release.set()
            if not finished.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError('WAL probe reader did not finish after release')
            if 'reader_error' in result:
                raise RuntimeError(result['reader_error'])
            wall = time.perf_counter()
            stage_owner(pending, changed, [], checkpoint=None)
            resumed = pending.publish(('synthetic-cumulative', 101), ready=True)
            result['resumed'] = dict(elapsed_ms=(time.perf_counter() - wall) * 1000,
                                      generation=resumed.generation, wal=store.disk_usage()['wal'])
            result['completed'] = True
        finally:
            release.set()
            thread.join(max(0, deadline - time.monotonic()))
            result['cleanup'] = not thread.is_alive()
            if thread.is_alive():
                raise TimeoutError('WAL probe owned reader thread is still alive')
    result['scope'] = 'Real PreparedQuery snapshot and writer admission with a deliberately scaled WAL limit. The coordinated pause is excluded from ordinary query statistics. This proves the mechanism, not a natural stall at the production WAL limit.'
    return result


def host_state():
    import ctypes
    class CoreInfo(ctypes.Structure):
        _fields_ = [('mask', ctypes.c_size_t), ('relationship', ctypes.c_uint32),
                    ('reserved', ctypes.c_byte * 16)]
    size = ctypes.c_uint32()
    kernel = ctypes.windll.kernel32
    kernel.GetLogicalProcessorInformation(None, ctypes.byref(size))
    cores = (CoreInfo * (size.value // ctypes.sizeof(CoreInfo)))()
    known_cores = kernel.GetLogicalProcessorInformation(cores, ctypes.byref(size))
    def cpu_times():
        idle, system, user = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
        if not kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
            raise ctypes.WinError()
        return idle.value, system.value + user.value
    before_idle, before_total = cpu_times()
    time.sleep(.1)
    after_idle, after_total = cpu_times()
    measured = dict(logical_cpus=os.cpu_count(),
                    physical_cores=sum(core.relationship == 0 for core in cores) if known_cores else None,
                    cpu_percent=100 * (1 - (after_idle - before_idle) / max(1, after_total - before_total)),
                    cpu_sample_seconds=.1, method='Windows GetSystemTimes and GetLogicalProcessorInformation')
    script = "$ErrorActionPreference='Stop'; [ordered]@{cpu=@(Get-CimInstance Win32_Processor | Select-Object NumberOfCores,NumberOfLogicalProcessors); use=Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter \"Name='_Total'\" | Select-Object PercentProcessorTime; queue=Get-CimInstance Win32_PerfFormattedData_PerfOS_System | Select-Object ProcessorQueueLength; processes=@(Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | Where-Object {$_.IDProcess -gt 0 -and $_.Name -ne '_Total'} | Sort-Object PercentProcessorTime -Descending | Select-Object -First 8 Name,IDProcess,PercentProcessorTime)} | ConvertTo-Json -Depth 6 -Compress"
    response = subprocess.run(['powershell', '-NoProfile', '-Command', script], text=True,
                              capture_output=True, timeout=25)
    if response.returncode:
        measured['cim_error'] = response.stderr.strip()
    else:
        measured['cim'] = json.loads(response.stdout)
    return measured


def main():
    parser = argparse.ArgumentParser(description='Run isolated PreparedStore/PreparedQuery scenarios against an explicit checkout.')
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--calls', type=int, default=24000)
    parser.add_argument('--owners', type=int, default=48)
    parser.add_argument('--width', type=int, default=256)
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--expected-head')
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--skip-wal', action='store_true')
    args = parser.parse_args()
    checkout, output = args.checkout.resolve(), args.output_dir.resolve()
    if not (checkout / 'cachemonitor').is_dir() or output.exists():
        parser.error('checkout must exist and output-dir must be a new path')
    if args.calls < args.owners * 4 or not 2 <= args.owners <= 128 or args.runs < 1:
        parser.error('require at least four calls per owner, two to 128 owners, and one run')
    if args.profile and args.runs != 1:
        parser.error('profile separately with --runs 1')
    output.mkdir(parents=True)
    sys.path.insert(0, str(checkout))
    os.environ['CODEXON_CPU_BACKEND'] = 'rust'
    os.environ['TZ'] = 'Asia/Seoul'
    if hasattr(time, 'tzset'):
        time.tzset()
    report = dict(completed=False, cleanup=False, errors=[], phase='identity', runs=[], proofs=[],
                  publications=[], profile=args.profile,
                  claim='Establish baseline latency and repeated work for exact cumulative comparison and performance queries. No improvement claim.',
                  scope='Synchronous PreparedQuery including generation adoption, engine creation, SQLite reads, aggregation, sorting, and Python result delivery. Fixture generation, storage seeding, output hashing, validation, file writes, GUI, QueryActor queueing, collection, and pipe serialization are excluded from each query timer.',
                  cache='Fresh handler and cloned SQLite file per repetition. Warm scenarios reuse the handler. OS file cache is uncontrolled.',
                  checkpoint='Freeze a deterministic scalar fixture and query matrix, prove counts and result reuse, measure query work, then profile separately.',
                  config=dict(calls=args.calls, owners=args.owners, width=args.width, runs=args.runs,
                              now=NOW, start=START, backend='rust'))
    payloads = {}
    profiler = cProfile.Profile() if args.profile else None
    started = time.perf_counter()
    try:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()
        if args.expected_head and head != args.expected_head:
            raise AssertionError(f'checkout is {head}, expected {args.expected_head}')
        from cachemonitor.native_backend import get_native
        import cachemonitor
        if not Path(cachemonitor.__file__).resolve().is_relative_to(checkout):
            raise AssertionError('imported another checkout')
        native = get_native()
        report['identity'] = dict(head=head, checkout=str(checkout), package=str(Path(cachemonitor.__file__).resolve()),
                                  status=subprocess.check_output(['git', 'status', '--short'], cwd=checkout, text=True).strip(),
                                  python=sys.version, platform=platform.platform(), timezone=time.tzname,
                                  utc_offset_seconds=-time.timezone, runtime=file_identity(sys.executable),
                                  requirements=file_identity(checkout / 'requirements-release.lock'),
                                  native=file_identity(native.__file__), script=file_identity(__file__))
        report['host_before'] = host_state()
        report['phase'] = 'fixture'
        population = fixture(args.calls, args.owners)
        report['fixture'] = dict(sha256=digest([asdict(owner) for owner in population]),
                                 owners=len(population), calls=sum(len(owner.rows) for owner in population),
                                 minimum_owner_calls=min(len(owner.rows) for owner in population),
                                 maximum_owner_calls=max(len(owner.rows) for owner in population),
                                 source='Direct scalar fixtures follow tests/test_prepared_analysis.py publication helpers. No production snapshots or user stores are opened. Append certificate follows tests/test_comparison_usage.py and certifies a byte-identical prefix.')
        base = output / 'seed.prepared.sqlite'
        wall = time.perf_counter()
        report['seed'] = dict(**seed(base, population), elapsed_ms=(time.perf_counter() - wall) * 1000)
        report['seed']['database'] = file_identity(base)
        for repetition in range(args.runs):
            database = output / f'run-{repetition:02d}.prepared.sqlite'
            shutil.copy2(base, database)
            run_suite(database, deepcopy(population), repetition, args.width, profiler, report, payloads)
        if not args.skip_wal and not args.profile:
            report['phase'] = 'wal-probe'
            database = output / 'wal-probe.prepared.sqlite'
            shutil.copy2(base, database)
            report['wal_probe'] = wal_probe(database, deepcopy(population), args.width)
        by_label = defaultdict(list)
        for run in report['runs']:
            by_label[run['label']].append(run)
        report['summary'] = {}
        for label, values in by_label.items():
            digests = {value['sha256'] for value in values}
            if len(digests) != 1:
                raise AssertionError(f'{label} changed its exact result between repetitions')
            times = [value['elapsed_ms'] for value in values]
            report['summary'][label] = dict(runs=len(values), median_ms=statistics.median(times),
                                           minimum_ms=min(times), maximum_ms=max(times), sha256=digests.pop(),
                                           calls=values[0]['calls'], work=values[0]['work'])
        report['phase'] = 'complete'
        report['completed'] = True
    except BaseException as error:
        report['errors'].append(dict(phase=report['phase'], type=type(error).__name__,
                                    message=str(error), trace=traceback.format_exc()))
    finally:
        if profiler is not None:
            profiler.dump_stats(output / 'query-profile.pstats')
            with (output / 'query-profile.txt').open('w', encoding='utf-8') as stream:
                pstats.Stats(profiler, stream=stream).sort_stats('cumulative').print_stats(60)
        report['host_after'] = host_state()
        report['whole_elapsed_ms'] = (time.perf_counter() - started) * 1000
        report['error_count'] = len(report['errors'])
        try:
            from cachemonitor.observer_state import ProcessLock
            for path in output.glob('*.writer.lock'):
                with ProcessLock(path):
                    pass
            report['cleanup'] = True
        except BaseException as error:
            report['errors'].append(dict(phase='cleanup', type=type(error).__name__, message=str(error)))
            report['error_count'] = len(report['errors'])
        with gzip.open(output / 'payloads.canonical.json.gz', 'wt', encoding='utf-8') as stream:
            json.dump(payloads, stream, ensure_ascii=False, separators=(',', ':'))
        (output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        print(json.dumps(dict(output=str(output), completed=report['completed'],
                              phase=report['phase'], errors=report['error_count'], cleanup=report['cleanup'])), flush=True)
    return 0 if report['completed'] and not report['errors'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
