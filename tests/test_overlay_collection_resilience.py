"""Optional quota persistence cannot suppress observed session/overlay usage."""
import copy
import sqlite3

import pytest

from cachemonitor import analysis_worker
from test_overlay import source


@pytest.mark.parametrize('failure_stage',['init','enrich','sync','import'])
def test_quota_storage_failure_keeps_publishing_real_calls(monkeypatch,tmp_path,failure_stage):
    session=source()
    original=copy.deepcopy(session)
    snapshots=[]
    class Index:
        path='unused-index.sqlite'
        def __init__(self,*args):pass
        def poll(self):
            return dict(ts=200,sessions=[copy.deepcopy(session)],homes=[session['home']],
                        errors=[],unassigned=[],index={'loading':False})
        def close(self):pass
    class Ledger:
        def __init__(self,*args):
            self.db=self
            if failure_stage=='init':raise sqlite3.DatabaseError('database disk image is malformed')
        def enrich_modes(self,snapshot):
            if failure_stage=='enrich':raise sqlite3.DatabaseError('database disk image is malformed')
        def sync(self,*args):
            if failure_stage=='sync':raise sqlite3.DatabaseError('database disk image is malformed')
        def import_observations(self,*args):
            if failure_stage=='import':raise sqlite3.DatabaseError('database disk image is malformed')
        def rollback(self):pass
        def close(self):pass
    class Connection:
        def poll(self,*args):return bool(snapshots)
        def recv(self):return {'kind':'stop'}
        def send(self,value):snapshots.append(value)
        def close(self):pass
    monkeypatch.setattr(analysis_worker,'UsageIndex',Index)
    monkeypatch.setattr(analysis_worker,'QuotaLedger',Ledger)
    analysis_worker.process_main(Connection(),['home'],tmp_path/'index.sqlite')
    assert len(snapshots)==1 and snapshots[0]['kind']=='snapshot'
    snapshot=snapshots[0]['value']
    assert snapshot['ledger_error']=='한도 기록 확인 지연 · 자동 재시도 중'
    assert not snapshot['errors']
    overlay=snapshot['overlay_sessions'][0]
    assert overlay['calls']==len(original['history']) and overlay['latest_key']==original['history'][-1]['key']
    assert [row['key'] for row in overlay['recent']]==[row['key'] for row in original['history']]
    assert session==original


def test_usage_ledger_retries_after_transient_failure(monkeypatch,tmp_path):
    from types import SimpleNamespace
    clock=[100.0]
    monkeypatch.setattr(analysis_worker,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    snapshots=[]
    writes=[]
    opens=[]
    session=source()
    class Index:
        path='unused'
        def __init__(self,*args):pass
        def poll(self):return dict(ts=clock[0],sessions=[copy.deepcopy(session)],homes=[session['home']],
            errors=[],unassigned=[],index={'loading':False})
        def close(self):pass
    class Ledger:
        def __init__(self,*args):self.db=self;opens.append(clock[0])
        def enrich_modes(self,*args):pass
        def sync(self,*args):
            writes.append(clock[0])
            if len(writes)==1:raise sqlite3.OperationalError('locked')
        def import_observations(self,*args):pass
        def rollback(self):pass
        def close(self):pass
    class Connection:
        def poll(self,timeout=0):
            if timeout:clock[0]+=1
            return len(writes)>=2
        def recv(self):return {'kind':'stop'}
        def send(self,value):snapshots.append(value)
        def close(self):pass
    monkeypatch.setattr(analysis_worker,'UsageIndex',Index)
    monkeypatch.setattr(analysis_worker,'QuotaLedger',Ledger)
    analysis_worker.process_main(Connection(),['h'],tmp_path/'index.sqlite')
    assert len(opens)==2 and opens[1]-opens[0]>=5
    assert snapshots[0]['value']['ledger_error']
    assert 'ledger_error' not in snapshots[-1]['value']
