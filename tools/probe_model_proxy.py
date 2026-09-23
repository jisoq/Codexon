"""Explicit live probe: one ephemeral Codex call, no persistent routing changes."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from cachemonitor.model_evidence import EvidenceStore
from cachemonitor.model_proxy import create_app


async def probe(args):
    report=Path(args.report).resolve()
    report.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cachemonitor-model-probe-') as folder:
        store=EvidenceStore(Path(folder)/'evidence.sqlite')
        home=os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))
        runner=web.AppRunner(create_app(store,home),access_log=None)
        await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0)
        await site.start()
        port=site._server.sockets[0].getsockname()[1]
        process=None
        try:
            command=[args.codex_exe,'exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
                     '--sandbox','read-only','--json','-m',args.model,
                     '-c',f'openai_base_url="http://127.0.0.1:{port}"',
                     '-c','model_reasoning_effort="low"',
                     'This is a transport smoke test. Do not call any tools. Reply exactly MODEL_PROXY_OK.']
            process=await asyncio.create_subprocess_exec(*command,cwd=folder,stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            output,errors=await asyncio.wait_for(process.communicate(),timeout=150)
            rows=store.db.execute('SELECT transport,response_id,requested_model,response_model,status,conflict '
                                  'FROM model_observations WHERE response_id<>? ORDER BY seq',('',)).fetchall()
            complete=[r for r in rows if r[4]=='completed']
            result={'exit_code':process.returncode,'marker_returned':b'MODEL_PROXY_OK' in output,
                    'completed':len(complete),'observations':[dict(zip(
                        ('transport','response_id','requested_model','response_model','status','conflict'),r)) for r in rows],
                    'passed':process.returncode==0 and bool(complete) and all(r[2]==r[3] and not r[5] for r in complete)}
            if not result['passed']:
                # Extract only known diagnostic markers; never retain raw child logs.
                result['diagnostics']=[marker for marker in ('401','403','429','502','WebSocket','websocket','HTTP','timeout',
                                                             'Unknown','unrecognized','unsupported')
                                       if marker.encode() in errors or marker.encode() in output]
            report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False))
            return 0 if result['passed'] else 1
        finally:
            if process is not None and process.returncode is None:
                process.kill();await process.communicate()
            await runner.cleanup()
            store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--codex-exe',required=True)
    parser.add_argument('--model',default='gpt-6-astra')
    parser.add_argument('--report',default='artifacts/model-proxy-live.json')
    sys.exit(asyncio.run(probe(parser.parse_args())))
