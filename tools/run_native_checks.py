"""Run installation QA in the real user's Windows environment and verify its exit."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_task import ObserverTask
from cachemonitor.observer_state import read_json
from cachemonitor.observer_control import atomic_write
from cachemonitor.install_dispatch import packaged_context
from tools.check_processes import register_root


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args();command=args.command
    if command and command[0]=='--':command=command[1:]
    if not command:parser.error('A command is required')
    output=args.output.resolve();report=output/'native-result.json'
    if args.worker:
        if packaged_context():raise RuntimeError('QA remained in a package context')
        with (output/'stdout.log').open('wb') as stdout,(output/'stderr.log').open('wb') as stderr:
            result=subprocess.run(command,stdout=stdout,stderr=stderr,cwd=Path(__file__).resolve().parents[1])
        atomic_write(report,json.dumps({'exit_code':result.returncode,'native':True}).encode())
        return result.returncode
    output.mkdir(parents=True,exist_ok=False)
    register_root(output)
    task=ObserverTask(str(output),role='NativeQA')
    task.start([sys.executable,'-B',str(Path(__file__).resolve()),'--worker','--output',str(output),'--',*command])
    deadline=time.monotonic()+600
    offsets={name:0 for name in ('stdout.log','stderr.log')}
    try:
        while time.monotonic()<deadline:
            state=task.inspect()
            for name,offset in offsets.items():
                log=output/name
                if log.exists():
                    with log.open('rb') as stream:
                        stream.seek(offset);chunk=stream.read(65536);offsets[name]=stream.tell()
                    print(chunk.decode('utf-8',errors='replace'),end='',flush=True)
            if not state.get('running') and state.get('state') not in (2,4) and state.get('last_result')!=267011:
                value=read_json(report)
                if not value or value.get('exit_code')!=state.get('last_result'):
                    raise RuntimeError('Native QA exit and receipt do not agree')
                print(json.dumps(value));return value['exit_code']
            time.sleep(1)
        (output/'deadline.json').write_text(json.dumps({'task':task.name,'state':state}))
        raise RuntimeError('Native QA exceeded its 600 second budget')
    finally:
        task.stop()
        task.remove()


if __name__=='__main__':raise SystemExit(main())
