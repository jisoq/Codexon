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
    task=ObserverTask(str(output),role='NativeQA')
    task.start([sys.executable,'-B',str(Path(__file__).resolve()),'--worker','--output',str(output),'--',*command])
    deadline=time.monotonic()+1800
    while time.monotonic()<deadline:
        state=task.inspect()
        if not state.get('running') and state.get('state') not in (2,4) and state.get('last_result')!=267011:
            task.remove();value=read_json(report)
            if not value or value.get('exit_code')!=state.get('last_result'):
                raise RuntimeError('Native QA exit and receipt do not agree')
            print(json.dumps(value));return value['exit_code']
        time.sleep(1)
    raise RuntimeError('Native QA still running; task and evidence retained')


if __name__=='__main__':raise SystemExit(main())
