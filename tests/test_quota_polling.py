import sqlite3
from types import SimpleNamespace

import pytest

from cachemonitor.quota_polling import QuotaPolling
from cachemonitor.quota_live import AccountClient
from cachemonitor.quota_cycles import QuotaLedger
from cachemonitor import quota_service as module


@pytest.fixture(autouse=True)
def deterministic_accounting_worker(monkeypatch):
    """Keep the fake-clock tests deterministic; process isolation has its own tests."""
    from PySide6.QtCore import QObject, Signal
    from cachemonitor.quota_worker import QuotaComputation
    class InlineWorker(QObject):
        result=Signal(object)
        def __init__(self,path,home,on_result):
            super().__init__()
            self.engine=QuotaComputation(path,home,ledger_factory=lambda path:module.QuotaLedger(path))
            self.on_result=on_result
            self.sequence=0
        def request(self,kind,key=None,**payload):
            self.sequence+=1
            request=dict(kind=kind,id=self.sequence,key=key,**payload)
            self.retry(request)
            return request['id']
        def retry(self,request):
            result=self.engine.execute(request)
            self.on_result(result)
        def close(self):self.engine.close()
    monkeypatch.setattr(module,'QuotaWorker',InlineWorker)


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
        def set(self):pass
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


def test_hidden_reports_keep_recording_and_resume_with_current_history(tmp_path,monkeypatch):
    clock=[1000.0];builds=[];lookups=[];updates=[]
    fake_time=SimpleNamespace(time=lambda:clock[0],monotonic=lambda:clock[0])
    monkeypatch.setattr(module,'time',fake_time)
    from cachemonitor import quota_tracking_store
    monkeypatch.setattr(quota_tracking_store,'time',fake_time)
    class Client:
        def __init__(self,*args):pass
        def close(self):pass
        def fetch(self):
            lookups.append(clock[0])
            return dict(source='live',account='test',bucket='codex',plan_type='pro',
                requested_at=clock[0],observed_at=clock[0],elapsed=0,
                windows={'weekly':dict(used_percent=20+len(lookups),resets_at=20000,window_minutes=10080)})
    class Ledger(QuotaLedger):
        def report(self,home):
            builds.append(clock[0]);return super().report(home,now=clock[0])
    monkeypatch.setattr(module,'AccountClient',Client)
    monkeypatch.setattr(module,'QuotaLedger',Ledger)
    service=module.QuotaService(tmp_path/'home',tmp_path/'index.sqlite',reports_enabled=False)
    service.updated.connect(lambda value:updates.append((clock[0],value)))
    class Wake:
        def clear(self):pass
        def set(self):pass
        def wait(self,seconds):
            clock[0]+=seconds
            if clock[0]==1007:service.set_reports_enabled(True)
            if clock[0]==1012:service.set_reports_enabled(False)
    service.wake=Wake()
    service.isInterruptionRequested=lambda:clock[0]>=1032
    service.run()
    assert builds and builds[0]==1007 and all(1007<=at<1012 for at in builds)
    assert any(at>=1012 and value['quota']['observed_at']>=1012 for at,value in updates)
    assert all('report' not in value for at,value in updates if at<1007 or at>=1012)
    resumed=next(value['report'] for at,value in updates if at>=1007 and 'report' in value)
    assert resumed['at']==1007 and resumed['worker_revision'] and 'view' in resumed
    with sqlite3.connect(service.path) as db:
        saved=[row[0] for row in db.execute('select received from tracking_observations order by received')]
    assert saved==lookups


def test_frequent_observations_publish_and_save_without_rebuilding_each_time(tmp_path,monkeypatch):
    clock=[1000.0];builds=[];updates=[]
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:clock[0],monotonic=lambda:clock[0]))
    class Client:
        def __init__(self,*args):pass
        def close(self):pass
    class Ledger(QuotaLedger):
        def report(self,home):
            builds.append(clock[0]);return super().report(home,now=clock[0])
    monkeypatch.setattr(module,'AccountClient',Client)
    monkeypatch.setattr(module,'QuotaLedger',Ledger)
    service=module.QuotaService(tmp_path/'home',tmp_path/'index.sqlite',live=False)
    service.updated.connect(lambda value:updates.append((clock[0],value)))
    def supply():
        service.supply_local(dict(observed_at=clock[0],account='a',plan_type='pro',
            windows={'weekly':dict(used_percent=clock[0]-1000,resets_at=20000,window_minutes=10080)}))
    class Wake:
        def clear(self):pass
        def set(self):pass
        def wait(self,seconds):
            clock[0]+=seconds
            if clock[0]<1009:supply()
    service.wake=Wake()
    supply()
    service.isInterruptionRequested=lambda:clock[0]>=1012
    service.run()
    assert builds==[1000,1005,1010]
    assert [value['quota']['observed_at'] for at,value in updates if at<1009]==list(range(1000,1009))
    assert updates[-1][1]['report']['at']==1010
    with sqlite3.connect(service.path) as db:
        assert [row[0] for row in db.execute('select at from observations order by at')]==list(range(1000,1009))


@pytest.mark.parametrize('failure', ['none', 'write_once', 'report_once', 'write_forever', 'write_readonly', 'report_forever'])
def test_exclusion_edits_refresh_locally_and_restore_without_changing_live_quota(tmp_path,monkeypatch,failure):
    from cachemonitor import quota_cycles, quota_tracking_store as tracking
    from cachemonitor.quota_cycles import quota_statistics
    from test_quota_tracking_integration import activity, observe
    clock=[1000.0];lookups=[];updates=[];attempts=[];report_failures=[]
    fake_time=SimpleNamespace(time=lambda:clock[0],monotonic=lambda:clock[0])
    monkeypatch.setattr(module,'time',fake_time)
    monkeypatch.setattr(tracking,'time',fake_time)
    monkeypatch.setattr(quota_cycles,'time',fake_time)
    service=module.QuotaService(tmp_path/'home',tmp_path/'index.sqlite')
    seed=QuotaLedger(service.path)
    tracking.enable(seed.db,service.home,900)
    activity(seed,901,{},home=service.home)
    observe(seed,902,70,home=service.home)
    turns={'one':dict(started_at=903,ended_at=908)}
    activity(seed,909,turns,[907],home=service.home)
    observe(seed,910,69,home=service.home)
    observe(seed,912,65,home=service.home)
    turns['two']=dict(started_at=914,ended_at=918)
    activity(seed,919,turns,[907,917],home=service.home)
    observe(seed,920,64,home=service.home)
    activity(seed,921,turns,[907,917],home=service.home)
    seed.close()
    class Ledger(QuotaLedger):
        def exclude_interval(self,*args):
            attempts.append(clock[0])
            if failure=='write_forever' or failure=='write_once' and len(attempts)==1:
                raise sqlite3.OperationalError('synthetic temporarily locked exclusion')
            if failure=='write_readonly':
                self.db.execute('pragma query_only=on')
            return super().exclude_interval(*args)
        def report(self,home):
            if attempts and (failure=='report_forever' or failure=='report_once' and not report_failures):
                report_failures.append(clock[0])
                raise sqlite3.OperationalError('synthetic temporarily locked report')
            return super().report(home,now=clock[0])
    class Client:
        def __init__(self,*args):pass
        def close(self):pass
        def fetch(self):
            lookups.append(clock[0])
            return dict(source='live',account='a',bucket='codex',plan_type='pro',
                requested_at=clock[0],observed_at=clock[0],elapsed=0,
                windows={'weekly':dict(used_percent=36,resets_at=1000000,window_minutes=10080)})
    monkeypatch.setattr(module,'QuotaLedger',Ledger)
    monkeypatch.setattr(module,'AccountClient',Client)
    service.updated.connect(updates.append)
    phase=[0]
    class Wake:
        def clear(self):pass
        def set(self):pass
        def wait(self,seconds):
            clock[0]+=seconds
            results=[v['exclusion_result'] for v in updates if 'exclusion_result' in v]
            if phase[0]==0:
                service.request_exclusion(910.1,920.1,'검토용 실행')
                assert attempts==[]  # The GUI-side method must not open the ledger.
                phase[0]=1
            elif phase[0]==1 and results and results[0]['ok']:
                service.restore_exclusion(results[0]['id']);phase[0]=2
            elif phase[0]==2 and len(results)==2:
                service.request_exclusion(920.1,910.1);phase[0]=3
    service.wake=Wake()
    service.isInterruptionRequested=lambda:clock[0]>=1045 or sum('exclusion_result' in v for v in updates)==3
    service.run()
    results=[v for v in updates if 'exclusion_result' in v]
    if failure in ('write_forever','write_readonly','report_forever'):
        assert len(results)==1
        result=results[0]['exclusion_result']
        assert not result['ok'] and result['error']
        # A timed-out or permanent failure must not write again behind the user's back.
        assert len(attempts)==(3 if failure=='write_forever' else 1)
        assert not results[0]['report'].get('exclusions')
        saved=failure=='report_forever'
        assert bool(result.get('saved'))==saved
        reopened=QuotaLedger(service.path)
        try:
            assert len(reopened.report(service.home,now=clock[0])['exclusions'])==int(saved)
            assert reopened.db.execute('select count(*) from calls').fetchone()[0]==2
            # Manual retry is available after an unsaved failure.
            if not saved:
                record=reopened.exclude_interval(service.home,910.1,920.1,'수동 재시도')
                assert record['id']
        finally:reopened.close()
        return
    assert len(results)==3
    assert [v['exclusion_result']['ok'] for v in results]==[True,True,False]
    assert [v['exclusion_result']['operation'] for v in results]==['exclude','restore','exclude']
    assert results[-1]['exclusion_result']['error'] and not results[-1]['issue']
    assert lookups==[1000]
    assert all(v['quota']==updates[0]['quota'] for v in updates)
    assert next(value['report'] for value in updates if 'report' in value)['view']['lifetime']['delta']==6
    assert results[0]['report']['view']['lifetime']['delta']==1
    assert results[0]['report']['exclusions'][0]['reason']=='검토용 실행'
    assert results[1]['report']['view']['lifetime']['delta']==6
    assert results[2]['report']['view']['lifetime']['delta']==6
    # A successful write followed by a failed report is acknowledged only after
    # the fresh report is ready, without replaying the write.
    if failure=='report_once':
        assert len(report_failures)==1
        assert len(attempts)==2  # The second attempt is the invalid selection.
        assert not any(v.get('exclusion_result') for v in updates if v['issue'])
    reopened=QuotaLedger(service.path)
    try:
        assert reopened.report(service.home,now=clock[0])['exclusions']==[]
        assert reopened.db.execute('select count(*) from calls').fetchone()[0]==2
        assert reopened.db.execute('select count(*) from quota_exclusions where restored_at is not null').fetchone()[0]==1
    finally:reopened.close()


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
        def set(self):pass
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
        previous=reports[reports.index(failed)-1]['report']
        assert failed['report'] is previous and previous['worker_revision'] and 'view' in previous
