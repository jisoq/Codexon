import sqlite3
from types import SimpleNamespace

import pytest

from cachemonitor.quota_polling import QuotaPolling
from cachemonitor.quota_live import AccountClient
from cachemonitor.quota_cycles import QuotaLedger
from cachemonitor import quota_service as module


def test_shared_local_quota_keeps_its_age_during_account_failure(tmp_path,monkeypatch):
    clock=[1000.0]
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:clock[0],monotonic=lambda:clock[0]))
    class Client:
        def __init__(self,*args):pass
        def fetch(self):raise ConnectionError('synthetic offline account')
        def close(self):pass
    monkeypatch.setattr(module,'AccountClient',Client)
    service=module.QuotaService(tmp_path/'home',tmp_path/'index.sqlite',tracking_enabled=False)
    observation=dict(home=service.home,observed_at=1000,plan_type='pro',
        windows={'weekly':dict(used_percent=25,resets_at=2000,window_minutes=10080)})
    service.supply_local(observation)
    reports=[];service.updated.connect(reports.append)
    class Wake:
        def clear(self):pass
        def wait(self,seconds):
            clock[0]+=5
            service.local_observations.put(observation)  # Delivery is not a fresh observation.
    service.wake=Wake()
    service.isInterruptionRequested=lambda:clock[0]>1130
    service.run()
    assert reports[0]['quota']['observed_at']==1000
    assert reports[0]['quota']['windows']['weekly']['used_percent']==25
    from cachemonitor.quota import quota_display
    assert quota_display(reports[-1]['quota'],'weekly',now=1130)['remaining'] is None
    assert all(r['quota']['observed_at']==1000 for r in reports)


def test_activity_is_coalesced_without_postponing_or_overriding_backoff():
    p=QuotaPolling()
    p.activity(('start',),True,0)
    p.finish(0,True)
    assert p.next_read==15
    for second in range(1,5):
        p.activity((second,),True,second)
        assert p.next_read==5 and not p.ready(second)
    assert p.ready(5)
    p.finish(5,False)
    for second in range(6,20):
        p.activity((second,),True,second)
        assert p.next_read==20
    p.finish(20,False)
    assert p.next_read==50
    for attempt in range(10):p.finish(100+attempt,False)
    assert p.next_read==409
    p.finish(410,True)
    assert p.next_read==425 and p.failures==0


@pytest.mark.parametrize('failure_stage',['startup','lock','report','lookup'])
def test_polling_recovers_and_preserves_history(tmp_path,monkeypatch,failure_stage):
    # Run real ledger SQL with a deterministic clock; no Codex/proxy requests.
    clock=[1000.0]
    fake_time=SimpleNamespace(time=lambda:clock[0],monotonic=lambda:clock[0])
    monkeypatch.setattr(module,'time',fake_time)
    from cachemonitor import quota_cycles, quota_tracking_store
    monkeypatch.setattr(quota_cycles,'time',fake_time)
    monkeypatch.setattr(quota_tracking_store,'time',fake_time)
    original_connect=sqlite3.connect
    def short_connect(*args,**kwargs):
        kwargs['timeout']=.01
        return original_connect(*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',short_connect)
    service=module.QuotaService(tmp_path/'home',tmp_path/'index.sqlite')
    service.tracking_after=1000
    reports=[]
    reads=[]
    faults=[]
    class Ledger(QuotaLedger):
        def __init__(self,*args):
            if failure_stage=='startup' and clock[0]<1010:
                raise sqlite3.OperationalError('startup temporarily locked')
            super().__init__(*args)
        def report(self,*args,**kwargs):
            if failure_stage=='report' and clock[0]>=1005 and not faults:
                faults.append(True)
                raise sqlite3.OperationalError('test report interrupted')
            return super().report(*args,**kwargs)
    class Client:
        process=None
        def __init__(self,*args):pass
        def close(self):pass
        def fetch(self):
            reads.append(clock[0])
            if failure_stage=='lookup' and len(reads)==2:
                raise ConnectionError('PRIVATE_SERVER_PAYLOAD')
            return dict(source='live',account='test',bucket='codex',plan_type='pro',
                requested_at=clock[0],observed_at=clock[0],elapsed=0,
                windows={'weekly':dict(used_percent=20+len(reads),resets_at=20000,window_minutes=10080)})
    monkeypatch.setattr(module,'AccountClient',Client)
    monkeypatch.setattr(module,'QuotaLedger',Ledger)
    locker=[None]
    class Wake:
        def clear(self):pass
        def wait(self,seconds):
            clock[0]+=seconds
            if failure_stage=='lock' and clock[0]==1014:
                locker[0]=original_connect(service.path)
                locker[0].execute('BEGIN IMMEDIATE')
            if locker[0] and clock[0]==1020:
                locker[0].rollback();locker[0].close();locker[0]=None
    service.wake=Wake()
    service.isInterruptionRequested=lambda:clock[0]>=1050
    service.updated.connect(reports.append)
    service.run()
    assert any(r['issue'] for r in reports)
    assert reports[-1]['issue']==''
    assert reports[-1]['quota']['observed_at']>1030
    with original_connect(service.path) as db:
        rows=db.execute('select received from tracking_observations order by received').fetchall()
        successful=[t for i,t in enumerate(reads) if failure_stage!='lookup' or i!=1]
        assert [r[0] for r in rows]==successful
        assert db.execute('pragma quick_check').fetchone()[0]=='ok'
    log=service.path.with_suffix('.diagnostics.log').read_text(encoding='utf-8')
    assert 'PRIVATE_SERVER_PAYLOAD' not in log
    if failure_stage=='lock':assert 'SQLITE_BUSY' in log
    # Temporary report failures keep the previously rendered history.
    if failure_stage=='report':
        failed=next(r for r in reports if r['issue'])
        assert failed['report']['history']
