"""Edits preserve the same accounting as an authoritative full read."""
import pytest
from cachemonitor import quota_tracking_store as tracking
from cachemonitor.quota_worker import QuotaComputation,chart_result,compact_summary
from cachemonitor.quota_view import prepare_quota_view
from test_quota_tracking_integration import activity,observe


@pytest.mark.parametrize('operation',('delete','exclude','restore','reset','reset_then_delete'))
def test_edit_reuses_archive_and_matches_full_history(tmp_path,monkeypatch,operation):
    monkeypatch.setattr('cachemonitor.quota_worker.time.time',lambda:170.)
    worker=QuotaComputation(tmp_path/'edits.sqlite','h')
    ledger=worker._ledger(writable=True)
    try:
        tracking.enable(ledger.db,'h',100)
        for at,remaining in ((102,70),(110,69),(120,100),(130,99),(140,98),(150,100),(160,99)):
            observe(ledger,at,remaining)
        calls=[107,127,137,157]
        wire=[dict(home='h',attempt=str(at),response_id=str(at),request_observed_at=at-2,
                   completed_observed_at=at,status='completed',ts=at) for at in calls]
        activity(ledger,161,{},calls,wire=wire)
        if operation=='reset_then_delete':observe(ledger,165,98)
        first=worker.execute(dict(kind='report',id=1))
        assert not first.get('error')
        full_reads=[]
        original=ledger.report
        def full(*args,**kwargs):
            full_reads.append(True)
            return original(*args,**kwargs)
        monkeypatch.setattr(ledger,'report',full)
        request=dict(kind='clear_cycle',id=2,start=120.1,account='a',delete=True)
        if operation=='reset_then_delete':
            reset=worker.execute(dict(kind='clear_cycle',id=2,start=150.1,account='a',delete=False))
            assert not reset.get('error')
            request['id']=3
        if operation in ('exclude','restore'):
            request=dict(kind='exclude',id=2,start=120.1,end=140.1,reason='test')
        if operation=='reset':request.update(start=150.1,delete=False)
        result=worker.execute(request)
        if operation=='restore':
            result=worker.execute(dict(kind='restore',id=3,exclusion_id=result['exclusion_result']['id']))
        assert not result.get('error')
        assert result['saved']
        assert not full_reads
        expected=prepare_quota_view(original('h',170),deferred=True)
        actual=result['report']['view']
        assert actual['lifetime']==compact_summary(expected['lifetime'])
        assert [p['start'] for p in actual['periods']]==[p['start'] for p in expected['periods']]
        for period in actual['periods']:
            selection=period['start']
            reply=worker.execute(dict(kind='chart',id=10,revision=result['report']['worker_revision'],selection=selection))
            rows=list(reply['value']['series']['rows'])
            reference=list(chart_result(expected,selection)['series']['rows'])
            assert rows==reference
        worker.close()
        worker=QuotaComputation(tmp_path/'edits.sqlite','h')
        reopened=worker.execute(dict(kind='report',id=20))
        assert reopened['report']['view']['lifetime']==actual['lifetime']
    finally:worker.close()


def test_saved_edit_survives_calculation_timeout_without_duplicate_write(tmp_path,monkeypatch):
    from cachemonitor.quota_cancellation import poll
    monkeypatch.setattr('cachemonitor.quota_worker.time.time',lambda:170.)
    worker=QuotaComputation(tmp_path/'retry.sqlite','h')
    ledger=worker._ledger(writable=True)
    token=poll.set(None)
    try:
        tracking.enable(ledger.db,'h',100)
        for at,remaining in ((102,70),(110,69),(120,100),(130,99),(140,98),(150,100),(160,99)):
            observe(ledger,at,remaining)
        activity(ledger,161,{},[107,127,157])
        worker.execute(dict(kind='report',id=1))
        request=dict(kind='exclude',id=2,start=120.1,end=140.1,reason='retry')
        def expired():raise TimeoutError('read deadline')
        def phase(stage):
            if stage=='reading':poll.set(expired)
        failed=worker.execute(request,phase)
        assert failed.get('error') and failed['saved'] and not failed['permanent']
        poll.set(None)
        completed=worker.execute(request)
        assert not completed.get('error') and completed['exclusion_result']['ok']
        db=worker._ledger(writable=True).db
        assert db.execute('SELECT count(*) FROM quota_exclusions WHERE reason=?',('retry',)).fetchone()[0]==1
    finally:poll.reset(token);worker.close()


def test_read_failure_reopens_closed_history_for_the_next_chart(tmp_path,monkeypatch):
    from cachemonitor.quota_cancellation import poll
    monkeypatch.setattr('cachemonitor.quota_worker.time.time',lambda:170.)
    worker=QuotaComputation(tmp_path/'read-retry.sqlite','h')
    ledger=worker._ledger(writable=True)
    token=poll.set(None)
    try:
        tracking.enable(ledger.db,'h',100)
        for at,remaining in ((102,70),(110,69),(120,100),(130,99),(140,98),(150,100),(160,99)):
            observe(ledger,at,remaining)
        activity(ledger,161,{},[107,127,157])
        first=worker.execute(dict(kind='report',id=1))
        assert not first.get('error')
        def expired():raise TimeoutError('read deadline')
        poll.set(expired)
        assert worker.execute(dict(kind='report',id=2)).get('error')
        poll.set(None)
        report=worker.execute(dict(kind='report',id=3))
        assert not report.get('error')
        chart=worker.execute(dict(kind='chart',id=4,revision=report['report']['worker_revision'],selection=120.1))
        assert not chart.get('error')
        assert len(chart['value']['series']['rows'])==3
    finally:poll.reset(token);worker.close()
