import sqlite3

from cachemonitor.quota_backlog import ObservationBacklog


def test_capacity_preserves_order_and_reopens_without_losing_distinct_observations(tmp_path):
    path=tmp_path/'quota.sqlite'
    backlog=ObservationBacklog(path,'home')
    backlog.MAX_ITEMS=2
    assert backlog.append('quota',{'observed_at':1})
    assert backlog.append('quota',{'observed_at':2})
    assert not backlog.available() and backlog.issue
    assert not backlog.append('quota',{'observed_at':3}) and backlog.unsaved is None
    assert backlog.close()
    restored=ObservationBacklog(path,'home')
    try:
        first=restored.first()
        assert first[1:] == ('quota',{'observed_at':1})
        # Leaving an entry unacknowledged keeps it replayable across interruption.
        restored.close()
        assert restored.first()==first
        restored.acknowledge(first[0])
        second=restored.first()
        assert second[1:]==('quota',{'observed_at':2})
        restored.acknowledge(second[0])
        assert restored.first() is None and restored.available()
        assert restored.db.execute('select items,bytes from budget').fetchone()==(0,0)
    finally:restored.close()


def test_failed_journal_write_keeps_one_record_and_blocks_further_acquisition(tmp_path,monkeypatch):
    backlog=ObservationBacklog(tmp_path/'quota.sqlite','home')
    original=backlog._store
    def fail(*args):raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(backlog,'_store',fail)
    assert backlog.append('quota',{'observed_at':1})
    assert not backlog.available()
    assert not backlog.append('quota',{'observed_at':2})
    assert not backlog.close()
    monkeypatch.setattr(backlog,'_store',original)
    assert backlog.close()
    restored=ObservationBacklog(tmp_path/'quota.sqlite','home')
    try:
        row=restored.first()
        assert row[1:]==('quota',{'observed_at':1})
        restored.acknowledge(row[0])
        assert restored.first() is None
    finally:restored.close()


def test_byte_budget_and_payload_limit_are_independent(tmp_path):
    backlog=ObservationBacklog(tmp_path/'quota.sqlite','home')
    backlog.MAX_ITEM_BYTES=64
    backlog.MAX_BYTES=100
    try:
        assert backlog.append('quota',{'value':'x'*40})
        assert not backlog.available()
        before=backlog.first()
        assert not backlog.append('quota',{'value':'x'*100})
        assert backlog.first()==before and backlog.unsaved is None
        assert not backlog.available() and backlog.rejected
    finally:backlog.close()


def test_control_reserve_retains_stop_boundary_when_observation_capacity_is_full(tmp_path):
    backlog=ObservationBacklog(tmp_path/'quota.sqlite','home')
    backlog.MAX_ITEMS=1
    backlog.CONTROL_RESERVE=1
    try:
        assert backlog.append('quota',{'observed_at':1})
        assert not backlog.available()
        assert backlog.append('control',(False,2))
        assert not backlog.append('control',(True,3))
        assert backlog.unsaved is None
        first=backlog.first();backlog.acknowledge(first[0])
        assert backlog.first()[1:]==('control',[False,2])
    finally:backlog.close()
