import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cachemonitor.model_evidence import EvidenceReader, EvidenceStore
from cachemonitor.notifications import ConfirmedNotifications
from cachemonitor.observer_control import ObserverManager


def clocked():
    clock=[100.]
    return ConfirmedNotifications(lambda:clock[0],lambda:clock[0]),clock


def test_http_fallback_requires_explicit_observation_and_never_replays():
    engine,clock=clocked();clock[0]=102
    source=dict(home='home',id='task',title='전환 확인',warning='HTTP 전환',
        history=[dict(key='response',transport='HTTP/SSE')],transports=[])
    assert engine.http([source])==[]
    observed=dict(ts=101,kind='HTTP/SSE',evidence='요청 경로 기록',turn='turn')
    source['transports']=[observed]
    assert engine.http([source])==[]
    observed['evidence']='HTTP 전환 기록'
    events=engine.http([source]);assert len(events)==1
    assert events[0]['target']['home']=='home'
    assert events[0]['target']['sid']=='task'
    assert events[0]['target']['request_id']=='turn'
    assert events[0]['target']['call_id'] is None
    assert events[0]['target']['filters']==('http',)
    assert engine.http([source])==[]
    engine.enable_http(False);clock[0]=103
    source['transports'].append({**observed,'ts':103})
    assert engine.http([source])==[]
    clock[0]=104;engine.enable_http(True)
    assert engine.http([source])==[]
    source['transports'].append({**observed,'ts':99})
    source['transports'].append({**observed,'ts':110})
    assert engine.http([source])==[]
    clock[0]=105;source['transports'].append({**observed,'ts':105})
    assert len(engine.http([source]))==1


def session(row):
    return dict(home='home',id='task',title='테스트 작업',history=[row])


def observed(tmp_path,status='completed',conflict=False,requested='asked',responded='reported'):
    store=EvidenceStore(tmp_path/'wire.sqlite');reader=EvidenceReader(store.path)
    store.write('home','attempt',101,'WebSocket','response',requested,responded,status,conflict)
    reader.poll()
    row=reader.enrich('home',[{'key':'response','ts':101}])[0]
    return store,reader,row


@pytest.mark.parametrize('status,conflict,requested,responded',[
    ('created',False,'asked','reported'),('pending',False,'asked','reported'),
    ('failed',False,'asked','reported'),('incomplete',False,'asked','reported'),
    ('disconnected',False,'asked','reported'),('completed',True,'asked','reported'),
    ('completed',False,'','reported'),('completed',False,'asked',''),
    ('completed',False,'same','same'),
])
def test_uncertain_or_unfinished_models_never_notify(tmp_path,status,conflict,requested,responded):
    engine,clock=clocked();clock[0]=102
    store,reader,row=observed(tmp_path,status,conflict,requested,responded)
    try:
        assert not row['model_alert_confirmed']
        assert engine.models([session(row)])==[]
        assert reader.enrich('other-home',[{'key':'response','ts':101}])[0]['model_alert_confirmed'] is False
    finally:reader.close();store.close()


def test_model_completion_conflict_and_pairing_are_checked_at_source(tmp_path):
    store,reader,row=observed(tmp_path,'created')
    try:
        store.write('home','attempt',101,'WebSocket','response','asked','reported','completed')
        reader.poll()
        assert reader.enrich('home',[{'key':'response'}])[0]['model_alert_confirmed']
        store.write('home','other',102,'WebSocket','response','different','reported','completed')
        reader.poll()
        assert not reader.enrich('home',[{'key':'response'}])[0]['model_alert_confirmed']
        store.write('home','req',102,'WebSocket','split','asked','','completed')
        store.write('home','res',102,'WebSocket','split','','reported','completed')
        reader.poll()
        assert not reader.enrich('home',[{'key':'split'}])[0]['model_alert_confirmed']
    finally:reader.close();store.close()


def test_models_once_per_response_grouped_for_ten_minutes_and_no_old_replay(tmp_path):
    engine,clock=clocked();clock[0]=102
    store,reader,row=observed(tmp_path)
    try:
        events=engine.models([session(row)])
        assert len(events)==1 and '요청: asked → 응답: reported' in events[0]['detail']
        assert engine.models([session(dict(row))])==[]
        assert engine.models([session({**row,'key':'second'})])==[]
        assert engine.records[0]['count']==2
        clock[0]=703
        assert engine.models([session(dict(row))])==[]
        assert len(engine.models([session({**row,'key':'third','ts':703,'model_observation_ts':703})]))==1
        engine.enable_model(False)
        clock[0]=704
        assert not engine.models([session({**row,'key':'disabled','ts':704,'model_observation_ts':704})])
        clock[0]=705;engine.enable_model(True)
        assert not engine.models([session({**row,'key':'disabled','ts':704,'model_observation_ts':704})])
        assert not engine.models([session({**row,'key':'old','ts':99,'model_observation_ts':99})])
        assert not engine.models([session({**row,'key':'future','ts':999,'model_observation_ts':999})])
        restarted,_=clocked()
        restarted.model_since=1000
        assert not restarted.models([session(row)])
    finally:reader.close();store.close()


@pytest.mark.parametrize('state',['refused','identity_mismatch'])
def test_proxy_three_spaced_confirmations_one_incident_and_recovery(state):
    engine,clock=clocked();result=dict(configured=True,probe_state=state)
    assert not engine.proxy(result)
    clock[0]=114;assert not engine.proxy(result)
    clock[0]=115;assert not engine.proxy(result)
    clock[0]=130;assert len(engine.proxy(result))==1
    clock[0]=145;assert not engine.proxy(result)
    assert not engine.proxy(dict(configured=True,probe_state='unknown'))
    clock[0]=160;assert not engine.proxy(result)
    assert not engine.proxy(dict(configured=True,probe_state='healthy'))
    assert engine.records[0]['resolution']=='정상 식별 응답 확인'
    for stamp in (175,190):
        clock[0]=stamp;assert not engine.proxy(result)
    clock[0]=205;assert len(engine.proxy(result))==1


def test_proxy_unknown_disabled_and_interrupted_checks_do_not_count():
    engine,clock=clocked();result=dict(configured=True,probe_state='refused')
    for stamp in (100,115):
        clock[0]=stamp;assert not engine.proxy(result)
    clock[0]=130;assert not engine.proxy(dict(configured=True,probe_state='unknown'))
    clock[0]=145;assert not engine.proxy(result)
    clock[0]=200;assert not engine.proxy(result)  # Old confirmations expire after a missed interval.
    assert not engine.proxy(dict(configured=False,probe_state='refused'))
    engine.enable_proxy(False)
    for stamp in (215,230,245):
        clock[0]=stamp;assert not engine.proxy(result)
    engine.enable_proxy(True)
    assert not engine.proxy(result)
    assert not engine.records


@contextmanager
def health_server(body,status=200,headers=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header('Content-Length',str(len(body)))
            for key,value in (headers or {}).items():self.send_header(key,value)
            self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    class HealthServer(ThreadingHTTPServer):
        def shutdown_request(self,request):
            # A FIN before urllib consumes the declared body can reset Windows
            # loopback reads, even with a graceful half-close. Content-Length
            # lets the client finish and close first; wait for that EOF rather
            # than sending an early FIN. The timeout bounds a broken test peer.
            try:
                request.settimeout(1)
                while request.recv(8192):pass
            except OSError:
                pass
            finally:
                super().shutdown_request(request)
    server=HealthServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}'
    finally:server.shutdown();server.server_close();thread.join()


def test_real_local_health_larger_than_16kb_and_classified_errors(tmp_path,monkeypatch):
    import hashlib
    from cachemonitor.model_evidence import home_key
    control=ObserverManager(tmp_path/'home',tmp_path/'data')
    identity=hashlib.sha256((home_key(control.home)+'|'+str(control.evidence.resolve())).encode()).hexdigest()
    health=dict(service='cachemonitor-model-observer',identity=identity,status='ok',instance='local',padding='x'*20000)
    with health_server(json.dumps(health).encode()) as url:
        monkeypatch.setattr(control,'url',url)
        assert control.health()==health and control.health_state=='healthy'
        with health_server(b'',302,{'Location':url+'/health'}) as redirect:
            monkeypatch.setattr(control,'url',redirect)
            assert control.health() is None and control.health_state=='unknown'
    for body,status in [(b'not JSON',200),(b'{}',200),(b'x'*262145,200),(b'error',500)]:
        with health_server(body,status) as url:
            monkeypatch.setattr(control,'url',url)
            assert control.health() is None and control.health_state=='unknown'
    with health_server(json.dumps({**health,'identity':'different'}).encode()) as url:
        monkeypatch.setattr(control,'url',url)
        with pytest.raises(RuntimeError):control.health()
        assert control.health_state=='identity_mismatch'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    monkeypatch.setattr(control,'url',f'http://127.0.0.1:{port}')
    assert control.status()['probe_state']=='refused'


def test_timeouts_are_unknown_not_refused(tmp_path,monkeypatch):
    import urllib.error
    control=ObserverManager(tmp_path/'home',tmp_path/'data')
    class Opener:
        def open(self,*args,**kwargs):raise urllib.error.URLError(TimeoutError('timeout'))
    monkeypatch.setattr('cachemonitor.observer_control.urllib.request.build_opener',lambda *_:Opener())
    assert control.health() is None and control.health_state=='unknown'


@pytest.mark.parametrize('code',[111,10061])
def test_explicit_os_connection_refusal_is_preserved(tmp_path,monkeypatch,code):
    import urllib.error
    control=ObserverManager(tmp_path/'home',tmp_path/'data')
    class Opener:
        def open(self,*args,**kwargs):raise urllib.error.URLError(ConnectionRefusedError(code,'refused'))
    monkeypatch.setattr('cachemonitor.observer_control.urllib.request.build_opener',lambda *_:Opener())
    assert control.health() is None and control.health_state=='refused'
