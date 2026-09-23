"""Opt-in four-call direct/relay comparison; never changes global routing.

Only sanitized transport metadata and usage totals are persisted. Prompts,
credentials, HTTP headers and raw child output are deliberately excluded.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp import ClientSession, web
from cachemonitor.model_evidence import EvidenceStore
from cachemonitor.model_proxy import create_app, proxy_loop


async def diagnose(args):
    report=Path(args.report).resolve()
    if report.exists():raise ValueError('Choose a new report path; existing diagnostics are preserved')
    report.parent.mkdir(parents=True,exist_ok=True)
    home=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    config=home/'config.toml'
    before=hashlib.sha256(config.read_bytes()).hexdigest()
    traces=[];results=[]
    with tempfile.TemporaryDirectory(prefix='cachemonitor-diagnostics-') as folder:
        store=EvidenceStore(Path(folder)/'evidence.sqlite')
        runner=web.AppRunner(create_app(store,home,diagnostics=traces.append),access_log=None)
        await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        proxy=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        try:
            for mode in ('direct','proxy','proxy','direct'):
                trace_start=len(traces)
                offset=store.db.execute('select coalesce(max(seq),0) from model_observations').fetchone()[0]
                endpoint=proxy if mode=='proxy' else 'https://chatgpt.com/backend-api/codex'
                command=[args.codex_exe,'exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
                         '--sandbox','read-only','--json','-m',args.model,
                         '-c',f'openai_base_url="{endpoint}"','-c','model_reasoning_effort="low"',
                         'Transport diagnostic. Do not use tools. Reply exactly DIAGNOSTIC_OK.']
                started=time.monotonic()
                process=subprocess.Popen(command,cwd=folder,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    env={**os.environ,'CODEX_HOME':str(home)},
                    **({'creationflags':0x08000000} if os.name=='nt' else {}))
                try:output,errors=await asyncio.to_thread(process.communicate,timeout=90)
                except BaseException:
                    if process.poll() is None:process.kill();await asyncio.to_thread(process.communicate)
                    raise
                usage=[];thread_id=None;turns=0
                for line in output.splitlines():
                    try:event=json.loads(line)
                    except (ValueError,UnicodeError):continue
                    if event.get('type')=='thread.started':thread_id=event.get('thread_id')
                    if event.get('type')=='turn.completed':
                        turns+=1
                        usage.append({k:v for k,v in event.get('usage',{}).items()
                                      if 'token' in k and isinstance(v,int)})
                # Let closed relay tasks publish their final diagnostic record.
                await asyncio.sleep(.1)
                observations=store.db.execute('''select transport,requested_model,response_model,status
                    from model_observations where seq in
                    (select max(seq) from model_observations where seq>? group by attempt)''',(offset,)).fetchall()
                result={'mode':mode,'exit_code':process.returncode,'thread_id':thread_id,
                        'elapsed_ms':round((time.monotonic()-started)*1000,3),'turns_completed':turns,
                        'marker_returned':b'DIAGNOSTIC_OK' in output,'usage':usage,
                        'stderr_markers':{word:errors.lower().count(word.encode())
                            for word in ('falling back','retrying','stream disconnected')},
                        'traces':traces[trace_start:],'observations':observations}
                results.append(result)
                report.write_text(json.dumps({'results':results,'finished':False},indent=2),encoding='utf-8')
                print(json.dumps({'mode':mode,'exit_code':process.returncode,'usage':usage,
                                  'completed':turns,'traces':len(result['traces'])}),flush=True)
                if process.returncode or turns!=1 or not result['marker_returned']:
                    raise RuntimeError('Diagnostic failed; no extra calls or automatic reruns were made')
            async with ClientSession() as client:
                async with client.get(proxy+'/health') as response:health=await response.json()
            assert hashlib.sha256(config.read_bytes()).hexdigest()==before
            report.write_text(json.dumps({'results':results,'finished':True,'config_unchanged':True,
                                          'health':health},indent=2),encoding='utf-8')
        finally:await runner.cleanup();store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-exe',required=True)
    parser.add_argument('--model',default='gpt-6-astra')
    parser.add_argument('--report',required=True)
    with asyncio.Runner(loop_factory=proxy_loop) as runner:
        runner.run(diagnose(parser.parse_args()))
