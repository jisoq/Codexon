"""Compare Python reference and Rust CPU paths on identical isolated workloads.

No monitoring clocks, production databases, network calls, or live processes
are involved. Timings include Python/Rust conversion and result construction.
"""
import argparse
from collections import deque
import gc
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture_rows(count):
    return [dict(ts=1800000000+i*3,home='synthetic',sid=str(i%40),key=str(i),turn=str(i//4),
                 model=('gpt-6-astra','gpt-6-sol','gpt-6-luna')[i%3],service_tier=('Standard','Fast')[i%2],
                 effort='high',cost=(i%17+1)/100,input=1000+i%701,cached=800+i%199,written=0,
                 uncached=200,ordinary_input=200,output=100+i%101,reasoning=50+i%31,non_reasoning=50,
                 total=1100+i%802,duration=1+i%7/10,completion_latency_ms=1000+i%700,
                 output_speed=(100+i%101)/(1+i%700/1000),response_status='completed') for i in range(count)]


def quota_cycle(count):
    points=[(100+i*30,i//5) for i in range(count*5)]
    rows=[dict(uid=str(i),ts=100+i*150+45,request_start=100+i*150+10,
               model='m',service_tier='Standard',cost=1.) for i in range(count)]
    return dict(id='cycle',start=points[0][0],end=points[-1][0],endpoints=points,cost_rows=rows,
                models=[dict(model='m',service_tier='Standard')],separate_models=[],
                delta=points[-1][1],forward_tracking=True,cost_complete=True,provisional=False)


def workloads(count):
    from cachemonitor.core import summarize, group_usage, Session, Transport
    from cachemonitor.performance_trends import PerformanceTrends
    from cachemonitor.proxy_observation import message_metadata
    from cachemonitor.index import _event_lines
    from cachemonitor.quota_view import prepare_series
    from cachemonitor.quota_attribution import local_request_intervals
    from cachemonitor.quota_cycles import _clip_quota_interval, _quota_clipper
    from cachemonitor.analysis_engine import AnalysisEngine
    rows=fixture_rows(count)
    source=SimpleNamespace(revision=1,sessions={'s':{'prepared':{'history':rows}}})
    query=dict(now=rows[-1]['ts']+30,plot_width=768)
    payload=json.dumps(dict(model='gpt-6-astra',service_tier='priority',stream=True,
                            input=[dict(role='user',content=[dict(type='input_text',text='synthetic')]) for _ in range(count)])).encode()
    lines=(b'{"type":"response_item","payload":{"text":"synthetic private content"}}\n'*9+
           b'{"type":"event_msg","payload":{"type":"token_count"}}\n')*count
    transport_count=min(2000,count)
    session=Session('s','synthetic')
    session.transports=deque(Transport(i*2,('WebSocket','HTTP/SSE')[i%2],turn=str(i%8)) for i in range(transport_count))
    requests=[SimpleNamespace(ts=i*2+1,turn=str(i%8)) for i in range(transport_count)]
    quota_rows=[dict(at=1800000000+i*30,remaining=100-(i//10)%100,connect=i%200!=0,
                     tracking_continuous=i%200!=0,reset_kind=None,cycle_start=i//1000,account='synthetic',
                     cycle_cost=i%1000/10,cycle_value=100+i%21) for i in range(count)]
    cycle=quota_cycle(min(2000,max(20,count//10)))
    ingest_rows=rows[:min(4000,count)]
    sessions=[dict(home='synthetic',id=str(i),title='Synthetic',cwd='C:\\synthetic',source='cli',archived=False,
                   history=[dict(row,sid=str(i)) for row in ingest_rows[i::20]],turn_states={},turn_records={}) for i in range(20)]

    def scan():
        stream=io.BytesIO(lines);digest=hashlib.sha256();selected=[]
        while stream.tell()<len(lines):
            before=stream.tell();selected.extend(_event_lines(stream,digest))
            if stream.tell()==before:break
        return dict(offset=stream.tell(),digest=digest.hexdigest(),selected=len(selected))

    def ingest():
        engine=AnalysisEngine();engine.ingest(sessions)
        return [state['prepared'] for state in engine.sessions.values()]

    return {
        'usage_summary':(lambda:summarize(rows),dict(rows=count)),
        'usage_groups':(lambda:group_usage(rows),dict(rows=count)),
        'transport_attribution':(lambda:session.transport_details_many(requests),dict(requests=transport_count,transports=transport_count)),
        'trends_calendar':(lambda:PerformanceTrends().query(source,dict(query,granularity='day')),dict(rows=count,plot_width=768)),
        'trends_rolling':(lambda:PerformanceTrends().query(source,dict(query,granularity='auto')),dict(rows=count,plot_width=768)),
        'proxy_metadata':(lambda:message_metadata(payload),dict(bytes=len(payload),ignored_items=count)),
        'rollout_scan':(scan,dict(bytes=len(lines),lines=count*10)),
        'quota_series':(lambda:prepare_series(quota_rows),dict(observations=count)),
        'quota_local_intervals':(lambda:local_request_intervals(cycle,[],_clip_quota_interval,_quota_clipper),dict(requests=len(cycle['cost_rows']),observations=len(cycle['endpoints']))),
        'analysis_ingest':(ingest,dict(rows=len(ingest_rows),sessions=len(sessions))),
    }


def compare(actual, expected, path='result', differences=None):
    if differences is None:differences=[]
    if actual is expected:return differences
    if isinstance(actual,dict) and isinstance(expected,dict):
        if actual.keys()!=expected.keys():raise AssertionError(f'{path}: keys differ')
        for key in actual:compare(actual[key],expected[key],f'{path}.{key}',differences)
    elif isinstance(actual,(list,tuple)) and isinstance(expected,(list,tuple)):
        if len(actual)!=len(expected):raise AssertionError(f'{path}: lengths differ')
        for i,(a,b) in enumerate(zip(actual,expected)):compare(a,b,f'{path}[{i}]',differences)
    elif isinstance(actual,(int,float)) and isinstance(expected,(int,float)) and not isinstance(actual,bool) and not isinstance(expected,bool):
        if actual != expected:
            if isinstance(actual,int) and isinstance(expected,int) or not math.isclose(actual,expected,rel_tol=1e-12,abs_tol=1e-12):
                raise AssertionError(f'{path}: {actual!r} != {expected!r}')
            differences.append(abs(actual-expected))
    elif actual != expected:raise AssertionError(f'{path}: {actual!r} != {expected!r}')
    return differences


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def measure(function, repetitions):
    gc.collect()
    cpu=time.process_time_ns();wall=time.perf_counter_ns()
    for _ in range(repetitions):result=function()
    wall=time.perf_counter_ns()-wall;cpu=time.process_time_ns()-cpu
    return dict(repetitions=repetitions,cpu_ms=cpu/1e6/repetitions,wall_ms=wall/1e6/repetitions)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows',type=int,default=20000)
    parser.add_argument('--samples',type=int,default=5)
    parser.add_argument('--min-cpu-ms',type=float,default=250)
    parser.add_argument('--only',nargs='+')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.rows<20 or args.samples<3 or args.min_cpu_ms<100:parser.error('Use rows >= 20, samples >= 3 and min-cpu-ms >= 100')
    from tools.build_native import checked_extension,source_digest,binary_digest
    binary=checked_extension()
    previous=os.environ.get('CODEXON_CPU_BACKEND')
    report=dict(schema=1,scope='isolated synthetic CPU workloads including boundary conversion',
                clock='process_time_ns CPU per call; perf_counter_ns elapsed per call',
                method='warm each backend; calibrated batches; alternate backend order; median across samples',
                parity_method='recursive value equality; equal int/float values accepted; differing floats checked at 1e-12 tolerance; serialized hashes reported separately',
                excluded='network, disk storage latency, GUI rendering, monitoring waits, production data',
                python=sys.version,platform=platform.platform(),processor=platform.processor(),
                rust=subprocess.check_output(['rustc','--version'],text=True).strip(),
                commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                native_source_sha256=source_digest(),native_binary_sha256=binary_digest(binary),
                samples=args.samples,minimum_batch_cpu_ms=args.min_cpu_ms,results={})
    try:
        for name,(function,size) in workloads(args.rows).items():
            if args.only and name not in args.only:continue
            values={};repeats={};samples={'python':[],'rust':[]}
            for backend in ('python','rust'):
                os.environ['CODEXON_CPU_BACKEND']=backend
                values[backend]=function()
                pilot=measure(function,1)
                repeats[backend]=min(1000,max(1,math.ceil(args.min_cpu_ms/max(pilot['cpu_ms'],pilot['wall_ms']*.7,.05))))
            differences=compare(values['rust'],values['python'])
            for sample in range(args.samples):
                for backend in (('python','rust') if sample%2==0 else ('rust','python')):
                    os.environ['CODEXON_CPU_BACKEND']=backend
                    samples[backend].append(measure(function,repeats[backend]))
            medians={backend:{metric:statistics.median(s[metric] for s in data) for metric in ('cpu_ms','wall_ms')} for backend,data in samples.items()}
            before=medians['python']['cpu_ms'];after=medians['rust']['cpu_ms']
            python_digest=digest(values['python']);rust_digest=digest(values['rust'])
            result=dict(input=size,parity=dict(exact_values=not differences,serialized_equal=python_digest==rust_digest,
                        float_tolerance=1e-12,max_absolute_difference=max(differences,default=0),
                        python_sha256=python_digest,rust_sha256=rust_digest),
                        median=medians,cpu_speedup=before/after if after else None,cpu_reduction_percent=(1-after/before)*100 if before else None,raw=samples)
            report['results'][name]=result
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(f'{name}: CPU {before:.3f} -> {after:.3f} ms ({result["cpu_speedup"]:.2f}x); exact={not differences}',flush=True)
    finally:
        if previous is None:os.environ.pop('CODEXON_CPU_BACKEND',None)
        else:os.environ['CODEXON_CPU_BACKEND']=previous
    return 0


if __name__=='__main__':raise SystemExit(main())
