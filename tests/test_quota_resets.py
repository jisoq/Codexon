"""Full-recovery events, chart continuity and durable pre/post-reset conversion."""
import pytest

from cachemonitor.quota import quota_display
from cachemonitor.quota_panel import history_rows, remaining_axis
from cachemonitor.quota_tracking import ResetTracker, Observation, TrackingState
from cachemonitor.quota_cycles import QuotaLedger, quota_statistics
from cachemonitor import quota_tracking_store as tracking
from test_quota_tracking_integration import activity


def point(at, remaining, reset=1000, **extra):
    return dict(id=str(at), window='weekly', at=at, used=100-remaining,
                reset=reset, minutes=10080, account='a', plan='pro', bucket='codex',
                separate='[]', source='live', **extra)


@pytest.mark.parametrize('at,expected', [(999,'arbitrary_reset'), (1000,'scheduled_reset'), (1001,'scheduled_reset')])
def test_recovery_uses_previously_collected_deadline(at, expected):
    tracker=ResetTracker()
    assert tracker.observe(70,950,1000) is None
    assert tracker.observe(100,at,605800)==expected
    assert tracker.observe(100,at+1,605800) is None


def test_advanced_schedule_recognizes_increase_before_full_sample():
    tracker=ResetTracker()
    tracker.observe(70,950,1000)
    assert tracker.observe(69,999,605800) is None
    assert tracker.observe(99.999,1001,605800)=='scheduled_reset'
    assert tracker.observe(100,1002,605800) is None


def test_first_post_reset_sample_at_99_starts_one_new_cycle():
    tracker=ResetTracker()
    assert tracker.observe(61,900,1000) is None
    assert tracker.observe(99,930,1000)=='arbitrary_reset'
    assert tracker.observe(100,940,1000) is None
    assert tracker.observe(98,950,1000) is None
    assert tracker.observe(99,960,1000)=='arbitrary_reset'


def test_realistic_long_gap_from_one_to_99_preserves_new_baseline():
    state=TrackingState(0)
    for seq,(at,remaining) in enumerate(((100,1),(1229,99),(1240,98)),1):
        assert state.observe(Observation(seq,at,at,remaining,('a','pro','codex',999999,())))
    assert state.closed[0]['reason']=='arbitrary_reset'
    assert state.baseline.remaining==99 and state.endpoint.remaining==98
    rows=history_rows({'account':'a','history':[point(100,1,999999),point(1229,99,999999),
                                                point(1240,98,999999)]},'weekly')
    assert rows[1]['reset_kind']=='arbitrary_reset'
    assert not rows[1]['connect']


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


def test_history_has_only_two_events_preserves_pre_filter_context_and_quiet_metadata():
    history=[point(900,80),point(930,75),point(950,100),point(970,95),point(1001,100,605800)]
    history[1].update(plan='plus',bucket='different',source='local',separate='["model"]')
    report={'history':history,'manual_resets':[{'at':920}]}
    rows=history_rows(report,'weekly')
    assert [r['markers'] for r in rows]==[[],[],['임의 초기화'],[],['정기 초기화']]
    assert all(r['connect'] for r in rows[1:])
    assert [r['remaining'] for r in rows]==[80,75,100,95,100]
    filtered=history_rows(report,'weekly',950,1001)
    assert filtered[0]['markers']==['임의 초기화'] and not filtered[0]['connect']
    assert filtered[-1]['markers']==['정기 초기화']
    report['tracking']={'controls':[{'at':940,'enabled':0},{'at':945,'enabled':1}]}
    rows=history_rows(report,'weekly')
    assert not rows[2]['markers'] and not rows[2]['connect']


def test_axis_fits_visible_remaining_values_and_keeps_full_recovery_visible():
    rows=history_rows({'history':[point(900,72),point(930,79)]},'weekly')
    low,high,step=remaining_axis(rows)
    assert 0<low<=72 and 79<=high<100 and step>0
    for value in (0,73,100):
        low,high,step=remaining_axis([{'remaining':value}])
        assert 0<=low<=value<=high<=100 and low<high
    assert remaining_axis(rows+[{'remaining':100}])[1]==100


def test_fresh_recovery_after_deadline_is_visible_even_before_next_schedule_arrives():
    q={'source':'live','observed_at':1001,'windows':{'weekly':{'used_percent':0,'resets_at':1000}}}
    assert quota_display(q,'weekly',1002)['remaining']==100
    q['observed_at']=999
    assert quota_display(q,'weekly',1002)['remaining'] is None


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
