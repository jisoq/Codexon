"""Full-recovery events, chart continuity and durable pre/post-reset conversion."""
import pytest

from cachemonitor.quota_panel import history_rows
from cachemonitor.quota_tracking import ResetTracker
from cachemonitor.quota_cycles import QuotaLedger, quota_statistics
from cachemonitor import quota_tracking_store as tracking
from test_quota_tracking_integration import activity


def point(at, remaining, reset=1000, **extra):
    return dict(id=str(at), window='weekly', at=at, used=100-remaining,
                reset=reset, minutes=10080, account='a', plan='pro', bucket='codex',
                separate='[]', source='live', **extra)


def test_first_post_reset_sample_at_99_starts_one_new_cycle():
    tracker=ResetTracker()
    assert tracker.observe(61,900,1000) is None
    assert tracker.observe(99,930,1000)=='arbitrary_reset'
    assert tracker.observe(100,940,1000) is None
    assert tracker.observe(98,950,1000) is None
    assert tracker.observe(99,960,1000)=='arbitrary_reset'


def test_same_deadline_can_only_cause_one_scheduled_reset():
    tracker=ResetTracker()
    tracker.observe(70,990,1000)
    assert tracker.observe(100,1000,1000)=='scheduled_reset'
    assert tracker.observe(95,1010,1000) is None
    assert tracker.observe(100,1020,1000)=='arbitrary_reset'


def test_first_sample_and_off_on_do_not_invent_resets_but_later_increase_counts():
    tracker=ResetTracker()
    assert tracker.observe(100,900,1000) is None
    tracker.observe(70,920,1000)
    assert tracker.observe(100,950,1000,continuous=False) is None
    tracker.observe(60,960,1000)
    assert tracker.observe(100,1200,605800)=='scheduled_reset'


@pytest.mark.parametrize('scheduled', [False,True])
def test_reset_keeps_both_conversions_and_raw_data_after_reopen(tmp_path,scheduled):
    path=tmp_path/'resets.sqlite'
    ledger=QuotaLedger(path)
    tracking.enable(ledger.db,'h',100)
    deadline=130 if scheduled else 1000000
    def observe(at,remaining,reset=deadline):
        q=dict(source='live',account='a',plan_type='pro',bucket='codex',requested_at=at,
               observed_at=at+.1,windows={'weekly':dict(used_percent=100-remaining,
               window_minutes=10080,resets_at=reset)})
        ledger.observe('h',q);tracking.observe(ledger.db,'h',q)
    observe(102,80);observe(120,60)
    activity(ledger,121,{},[115])
    ledger.db.execute('update calls set cost=12 where uid=?',('115',));ledger.db.commit()
    before=quota_statistics(ledger.report('h',122))
    assert before['delta']==20 and before['cost']==12
    # An arbitrary reset can retain the exact same server deadline.
    # The first observation after reset can already be below 100%.
    observe(130,99,deadline+604800 if scheduled else deadline)
    observe(135,100,deadline+604800 if scheduled else deadline)
    observe(150,95,deadline+604800 if scheduled else deadline)
    activity(ledger,151,{},[115,145])
    ledger.db.execute('update calls set cost=case uid when ? then 12 else 4 end',('115',));ledger.db.commit()
    report=ledger.report('h',152)
    stats=quota_statistics(report)
    assert stats['total']==2 and stats['delta']==25 and stats['cost']==16
    assert stats['per_percent']==pytest.approx(.64)
    old=next(r for r in stats['intervals'] if r['start']==102.1)
    assert old['delta']==before['delta'] and old['cost']==before['cost']
    assert old['per_percent']==before['per_percent']
    assert old['reason']==('scheduled_reset' if scheduled else 'arbitrary_reset')
    rows=history_rows(report,'weekly')
    assert [r['reset_kind'] for r in rows if r['reset_kind']]==[old['reason']]
    filtered=quota_statistics(report,130,152)
    assert filtered['delta']==5 and filtered['cost']==4
    counts=[ledger.db.execute('select count(*) from '+table).fetchone()[0]
            for table in ('calls','observations','tracking_observations')]
    ledger.close()
    ledger=QuotaLedger(path);tracking.enable(ledger.db,'h',153)
    again=quota_statistics(ledger.report('h',154))
    assert again['per_percent']==stats['per_percent'] and again['total']==2
    assert counts==[ledger.db.execute('select count(*) from '+table).fetchone()[0]
                    for table in ('calls','observations','tracking_observations')]
    tracking.enable(ledger.db,'h',155,False)
    assert quota_statistics(ledger.report('h',156))['per_percent']==stats['per_percent']
    ledger.close()
