"""Packaged update smoke: isolated home, evidence DB, port and scheduler tasks."""
import argparse
import asyncio
import threading
import json
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_control import ObserverManager
from cachemonitor.observer_task import ObserverTask
from cachemonitor.observer_state import read_json
from cachemonitor.model_evidence import home_key
from cachemonitor.proxy_update import ProxyUpdate, process_executable
from cachemonitor.version import PROXY_VERSION


class IdleConnections:
    """Loopback peers only; readers acknowledge cooperative CLOSE frames."""
    def __init__(self):
        self.ready=threading.Event();self.thread=threading.Thread(target=self.run,daemon=True)
        self.thread.start();assert self.ready.wait(10)
    def run(self):
        from cachemonitor.model_proxy import proxy_loop
        with asyncio.Runner(loop_factory=proxy_loop) as runner:runner.run(self.serve())
    async def serve(self):
        from aiohttp import web,ClientSession,TCPConnector
        self.loop=asyncio.get_running_loop();self.stop=asyncio.Event();self.sockets=[];self.readers=[]
        async def endpoint(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            async for _ in ws:pass
            return ws
        app=web.Application();app.router.add_get('/responses',endpoint)
        runner=web.AppRunner(app,access_log=None);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        self.url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
        async with ClientSession(connector=TCPConnector(limit=100)) as client:
            self.client=client;self.ready.set();await self.stop.wait()
            await asyncio.gather(*(ws.close() for ws in self.sockets))
            await asyncio.gather(*self.readers,return_exceptions=True)
        await runner.cleanup()
    def open(self,url,count):
        async def connect():
            async def read(ws):
                async for _ in ws:pass
            for _ in range(count):
                ws=await self.client.ws_connect(url+'/responses');self.sockets.append(ws)
                self.readers.append(asyncio.create_task(read(ws)))
        asyncio.run_coroutine_threadsafe(connect(),self.loop).result(30)
    def close(self):
        self.loop.call_soon_threadsafe(self.stop.set);self.thread.join(15)
        assert not self.thread.is_alive()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-exe',type=Path,required=True)
    parser.add_argument('--new-exe',type=Path,required=True)
    parser.add_argument('--port',type=int,default=18769)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cache-worker',action='store_true')
    parser.add_argument('--idle-connections',type=int,default=0)
    parser.add_argument('--expect-unsupported',action='store_true')
    parser.add_argument('--exercise-rollback',action='store_true')
    parser.add_argument('--interrupt-after-drain',action='store_true')
    options=parser.parse_args()
    if options.port in (8768,8771) or not 1024<=options.port<=65535:
        parser.error('운영 프록시 포트를 제외한 테스트 전용 포트를 지정하세요.')
    old=options.old_exe.resolve();new=options.new_exe.resolve()
    if not old.is_file() or not new.is_file():
        parser.error('두 배포 실행 파일이 모두 필요합니다.')
    root=options.output.resolve();root.mkdir(parents=True,exist_ok=True)
    old_report=root/'old-runtime.json'
    verified=subprocess.run([str(old),'--verify-runtime',str(old_report)],timeout=30)
    old_runtime=read_json(old_report)
    assert verified.returncode==0 and old_runtime.get('errors')==[]
    old_version=old_runtime.get('proxy_version',old_runtime['version'])
    home=root/'home';home.mkdir(exist_ok=True)
    m=ObserverManager(home,root/'data',url=f'http://127.0.0.1:{options.port}')
    if options.cache_worker:
        from cachemonitor.cache_worker_control import CacheWorkerManager
        from cachemonitor.observer_control import atomic_write
        index=root/'index'/'usage.sqlite';index.parent.mkdir(exist_ok=True)
        atomic_write(index.with_name('cache-route.json'),json.dumps(dict(url=m.url)).encode())
        m=CacheWorkerManager(home,index,m.evidence)
    updater=ObserverTask(home_key(home),role='ProxyUpdate')
    home.joinpath('config.toml').write_text(f'openai_base_url="http://127.0.0.1:{options.port}"\n')
    m.write_state(dict(home=home_key(home),enabled=True,phase='active',upstream='chatgpt'))
    args=['--codex-home',str(home),'--evidence-path',str(m.evidence),'--upstream','chatgpt','--port',str(options.port)]
    before=m.config_path.read_bytes()
    peers=IdleConnections()
    try:
        roles=['--model-proxy','--cache-worker','--observation-index',str(m.index)] if options.cache_worker else ['--proxy-supervisor']
        m.task.start([str(old),*roles,*args,'--upstream-url',peers.url],autostart=False)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            health=m.health(timeout=1)
            if health and (options.cache_worker or m.runtime().get('phase')=='active'):break
            time.sleep(.5)
        assert health and health['version']==old_version
        previous_instance=health['instance']
        from cachemonitor.proxy_identity import process_identity,same_process
        old_identity=process_identity(health['pid'])
        peers.open(m.url,options.idle_connections)
        assert m.health(timeout=3)['active_connections']==options.idle_connections
        previous=dict(version=health['version'],instance=health['instance'],process=old_identity,role='cache-worker' if options.cache_worker else 'observer')
        ProxyUpdate(m).publish('queued',source_instance=health['instance'],cancel_requested=False)
        updater_args=['--cache-worker','--observation-index',str(m.index)] if options.cache_worker else []
        if options.interrupt_after_drain:
            fixture=root/'interrupted_updater.py';marker=root/'updater-interrupted'
            constructor=(f"CacheWorkerManager({str(home)!r},{str(m.index)!r},{str(m.evidence)!r})" if options.cache_worker else
                         f"ObserverManager({str(home)!r},{str(m.directory)!r},url={m.url!r})")
            fixture.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0,"+repr(str(Path(__file__).resolve().parents[1]))+")\n"
                "from cachemonitor.observer_control import ObserverManager\n"
                "from cachemonitor.cache_worker_control import CacheWorkerManager\n"
                "from cachemonitor.proxy_update import ProxyUpdate\n"
                "sys.executable="+repr(str(new))+"\nupdater=ProxyUpdate("+constructor+")\noriginal=updater.drain\n"
                "marker=Path("+repr(str(marker))+")\n"
                "def drain(source):\n    original(source)\n    if not marker.exists():\n"
                "        marker.write_text('old process exited')\n        raise SystemExit(75)\n"
                "updater.drain=drain\nupdater.run()\n",encoding='utf-8')
            # Two launchers must still produce one replacement. Task Scheduler
            # restarts the failed independent updater from the same journal.
            updater.start([sys.executable,str(fixture)])
            updater.start([sys.executable,str(fixture)])
        elif options.exercise_rollback:
            fixture=root/'verification_failure.py'
            constructor=(f"CacheWorkerManager({str(home)!r},{str(m.index)!r},{str(m.evidence)!r})" if options.cache_worker else
                         f"ObserverManager({str(home)!r},{str(m.directory)!r},url={m.url!r})")
            fixture.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0,"+repr(str(Path(__file__).resolve().parents[1]))+")\n"
                "from cachemonitor.observer_control import ObserverManager\n"
                "from cachemonitor.cache_worker_control import CacheWorkerManager\n"
                "from cachemonitor.proxy_update import ProxyUpdate,PROXY_VERSION\n"
                "sys.executable="+repr(str(new))+"\nupdater=ProxyUpdate("+constructor+")\noriginal=updater.ready\ninjected=False\n"
                "def ready(*args,**kwargs):\n    global injected\n    result=original(*args,**kwargs)\n"
                "    if not injected and args[2]==PROXY_VERSION:\n        injected=True\n"
                "        updater.publish('verifying',rejected_instance=result['instance'])\n"
                "        raise RuntimeError('Isolated verification failure injection')\n    return result\n"
                "updater.ready=ready\nupdater.run()\n",encoding='utf-8')
            updater.start([sys.executable,str(fixture)])
        else:updater.start([str(new),'--proxy-update',*args,*updater_args])
        deadline=time.monotonic()+(150 if options.interrupt_after_drain else 75)
        while time.monotonic()<deadline:
            state=read_json(m.directory/'proxy-update.json')
            if state.get('phase') in ('complete','failed'):break
            time.sleep(.5)
        if options.expect_unsupported:
            assert state.get('phase')=='failed' and same_process(old_identity),state
            assert m.health(timeout=3)['active_connections']==options.idle_connections
            result=dict(phase='blocked',previous=previous,message=state['message'],old_process_preserved=True)
            (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result));return
        assert state.get('phase')==('failed' if options.exercise_rollback else 'complete'),state
        if options.interrupt_after_drain:assert marker.exists()
        until=time.monotonic()+5
        while time.monotonic()<until:
            registration=updater.inspect()
            if not registration.get('periodic'):break
            time.sleep(.2)
        assert registration['periodic'] is False and registration['execution_limit']=='PT0S'
        if options.exercise_rollback:assert state.get('restored') and state.get('rejected_instance'),state
        expected_version=old_version if options.exercise_rollback else PROXY_VERSION
        expected_executable=old if options.exercise_rollback else new
        deadline=time.monotonic()+20
        health=None
        while time.monotonic()<deadline:
            health=m.health(timeout=3)
            if health and health.get('version')==expected_version and health.get('status')=='ok':
                break
            time.sleep(.5)
        assert health and health['version']==expected_version and health['status']=='ok', (state,health)
        if not options.exercise_rollback:assert health.get('role')==('cache-worker' if options.cache_worker else 'observer')
        if not options.cache_worker:
            if not options.exercise_rollback:
                assert health.get('lifecycle')=='managed'
                assert m.runtime()['pid']==health['pid']
        assert health['instance']!=previous_instance
        assert Path(process_executable(health['pid'])).resolve()==expected_executable
        assert not same_process(old_identity)
        assert m.task.inspect()['autostart'] is False
        from cachemonitor.proxy_websocket import POLICY
        if not options.exercise_rollback:assert health['websocket_policy']==vars(POLICY)
        assert m.config_path.read_bytes()==before
        result={'phase':state['phase'],'previous':previous,'rollback_verified':options.exercise_rollback,'updater_restart_verified':options.interrupt_after_drain,'current':{**{k:health.get(k) for k in ('version','role','executable','instance','pid')},'executable':str(expected_executable),'role':'cache-worker' if options.cache_worker else 'observer'},'idle_connections_drained':options.idle_connections,'configuration_preserved':True,
                'instance_replaced':True,'new_executable_verified':True}
        (root/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
    finally:
        # Stop the isolated updater before taking the same control lock during
        # cleanup, including when a timed-out transition is still in progress.
        updater.stop()
        m.recover_direct()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if not m.health(timeout=3) and m.health_state=='refused':break
            time.sleep(.5)
        peers.close()
        m.task.stop();m.task.remove();updater.remove()
        if options.cache_worker:
            collector=ObserverTask(str(m.index.resolve()),role='UsageCollector');collector.stop();collector.remove()

if __name__=='__main__':main()
