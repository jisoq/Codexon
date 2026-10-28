from contextlib import closing
from copy import deepcopy
import sqlite3

import pytest

from cachemonitor.analytics import call_identity
from cachemonitor.mismatch_history import MismatchArchive, MismatchReader
from cachemonitor.prepared_store import Page
from cachemonitor.prepared_query import PreparedQuery


def call(key='resp-1', sid='owner', **changes):
    return dict(home='home', sid=sid, key=key, ts=100., model='gpt-6-astra',
                requested_model='gpt-6-astra', response_model='gpt-6-sol',
                model_alert_confirmed=True, effort='high', input=100, cached=40,
                written=0, output=20, reasoning=5, response_status='completed',
                timing_valid=True, completion_latency_ms=2000, **changes)


class Source:
    def __init__(self, *rows):
        self.rows=list(rows)

    def mismatch_shutdown(self, deadline):
        from contextlib import nullcontext
        return nullcontext()

    def mismatch_candidates(self, owners=None, *, after=None):
        selected={}
        for row in self.rows:
            owner=(row['home'],row['sid'])
            if row.get('model_alert_confirmed') and (owners is None or owner in owners):
                selected.setdefault(owner,[]).append(row['key'])
        return Page(tuple(dict(owner=owner,keys=tuple(keys)) for owner,keys in selected.items()),None,True,0,0)

    def mismatch_changed_identities(self, owners, *, after=None):
        values=tuple(call_identity(row['home'],row['sid'],row['key']) for row in self.rows
                     if (row['home'],row['sid']) in owners)
        return Page(values,None,True,0,0)

    def mismatch_occurrences(self, identity, *, after=None):
        rows=[deepcopy(row) for row in self.rows if call_identity(row['home'],row['sid'],row['key'])==identity]
        return Page(tuple(rows),None,True,0,0)


def capture(writer, source, **kwargs):
    import time
    result=writer.capture(source, **kwargs)
    deadline=time.monotonic()+5
    while writer.has_work:
        assert time.monotonic()<deadline
        result=writer.step()
    return result


def records(path):
    with closing(MismatchReader(path)) as reader:
        return reader.window()['window']['rows']


def test_capture_deduplicates_preserves_metadata_and_does_not_write_unchanged_poll(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    source=Source(call())
    with closing(MismatchArchive(path)) as writer:
        assert capture(writer,source)['status']=='ready'
        first=records(path)[0]
        revision=writer.revision
        assert first['rate']==40
        assert first['duration']==2
        assert first['output_speed']==10
        assert first['response_effort'] is None
        assert first['response_service_tier'] is None
        changes=writer.db.total_changes
        capture(writer,source,owners=frozenset({('home','owner')}))
        assert writer.db.total_changes==changes
        assert writer.revision==revision
        assert records(path)==[first]
        assert 'prompt' not in first
    with closing(MismatchArchive(path)) as writer:
        capture(writer,source)
        assert records(path)==[first]
        assert writer.revision==revision


def test_owner_movement_and_source_deletion_preserve_one_saved_call(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    source=Source(call())
    with closing(MismatchArchive(path)) as writer:
        capture(writer,source)
        source.rows=[call(sid='moved')]
        capture(writer,source,owners=frozenset({('home','owner'),('home','moved')}))
        saved=records(path)
        assert len(saved)==1 and saved[0]['sid']=='moved'
        source.rows=[]
        capture(writer,source,owners=frozenset({('home','moved')}))
        assert records(path)==saved
    with closing(PreparedQuery(tmp_path/'usage.prepared.sqlite')) as query:
        page=query(dict(kind='mismatch_page',id=1),None,lambda:False)
        assert page['window']['rows']==saved
        detail=query(dict(kind='mismatch_record',id=2,identity=saved[0]['identity']),None,lambda:False)
        assert detail['row']==saved[0]


def test_correction_and_reconfirmation_keep_last_confirmed_snapshot(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    source=Source(call())
    with closing(MismatchArchive(path)) as writer:
        capture(writer,source)
        source.rows[0].update(model_alert_confirmed=False,response_model='gpt-6-astra')
        capture(writer,source,owners=frozenset({('home','owner')}))
        corrected=records(path)[0]
        assert corrected['response_model']=='gpt-6-sol'
        assert corrected['correction_reason']=='model_agrees'
        assert corrected['correction_at'] is not None
        first=corrected['first_captured_at']
        source.rows=[call()]
        capture(writer,source)
        restored=records(path)[0]
        assert restored['correction_at'] is None
        assert restored['first_captured_at']==first


def test_failed_first_capture_retains_confirmation_and_later_correction_until_commit(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    holder=MismatchArchive(path)
    holder.retry(force=True)
    source=Source(call())
    try:
        with closing(MismatchArchive(path)) as writer:
            assert capture(writer,source)['status']=='retry_pending'
            assert records(path)==[]
            source.rows[0]['model_alert_confirmed']=False
            capture(writer,source)
            assert len(writer.pending)==1
            source.rows=[]
            capture(writer,source)
            holder.close()
            assert writer.retry(force=True)['status']=='ready'
            saved=records(path)[0]
            assert saved['requested_model']=='gpt-6-astra'
            assert saved['response_model']=='gpt-6-sol'
            assert saved['correction_reason']=='confirmation_withdrawn'
    finally:
        holder.close()


def test_never_confirmed_calls_are_not_archived_and_contradiction_is_visible(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    normal=call('ordinary')
    normal['model_alert_confirmed']=False
    with closing(MismatchArchive(path)) as writer:
        capture(writer,Source(normal))
        assert records(path)==[]
        other=call(sid='other')
        other['response_model']='gpt-6-luna'
        capture(writer,Source(call(),other))
        saved=records(path)
        assert len(saved)==1
        assert saved[0]['correction_reason']=='conflicting_occurrences'


def test_scoped_synthetic_calls_remain_distinct_and_windows_are_bounded(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    with closing(MismatchArchive(path)) as writer:
        capture(writer,Source(*(call(f'record:{n}',sid=sid) for n in range(260) for sid in ('a','b'))))
    with closing(MismatchReader(path)) as reader:
        page=reader.window(start=2,size=10000)
        assert page['window']['total']==520
        assert len(page['window']['rows'])==512
        assert page['window']['start']==2
        assert reader.record(('home','a','record:0'))['row']['sid']=='a'
        with pytest.raises(ValueError):
            reader.window(start=-1)


def test_pending_memory_has_a_visible_gap_and_recovers_surviving_sources(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    holder=MismatchArchive(path)
    holder.retry(force=True)
    source=Source(*(call(f'resp-{n}') for n in range(20)))
    try:
        with closing(MismatchArchive(path,pending_bytes=5000)) as writer:
            assert capture(writer,source)['status']=='capture_gap'
            assert writer.pending_bytes<=5000
            holder.close()
            writer.retry(force=True)
            capture(writer,source)
            assert writer.health()['gap']
            assert len(records(path))==20
    finally:
        holder.close()


def test_missing_archive_is_empty_but_broken_archive_is_an_error(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    with closing(MismatchReader(path)) as reader:
        assert reader.window()['window']==dict(start=0,total=0,rows=[])
        assert not path.exists()
    path.write_bytes(b'not sqlite')
    with closing(PreparedQuery(tmp_path/'usage.prepared.sqlite')) as query:
        result=query(dict(kind='mismatch_page',id=7),None,lambda:False)
        assert result['id']==7 and result.get('error')

def test_archive_failure_keeps_normal_writer_publication_live_and_retries_saved_confirmation(tmp_path):
    import time
    from cachemonitor.core import Session
    from cachemonitor.mismatch_history import archive_path
    from cachemonitor.prepared_runtime import PreparedWriter
    from cachemonitor.usage_collection import CollectionChannel, empty_snapshot

    home=tmp_path/'home'
    channel=CollectionChannel([home],tmp_path/'source.sqlite')
    quota=tmp_path/'usage.sqlite'
    blocked=archive_path(quota)
    blocked.mkdir()
    channel.start_live()
    session=Session('session',str(home),title='fixture')
    session.add_usage(100,'response',dict(input_tokens=100,cached_input_tokens=40,output_tokens=20),
                      'gpt-6-astra','turn','high',service_tier='Standard')
    source=session.view(200)
    source['history'][0].update(model_alert_confirmed=True,requested_model='gpt-6-astra',
                                response_model='gpt-6-sol',response_status='completed')

    def publish(sequence):
        now=time.time()
        snapshot=empty_snapshot(channel.homes,channel.path,now)
        snapshot.update(sessions=[source],catalog_scope='full',usage_collection_complete=True,last_usage_collection_success=now,
                        request_activity=[],quota_by_home={})
        snapshot['index'].update(loading=False,usage_complete=True,last_usage_success=now)
        channel.publish(snapshot,'mismatch-runtime',sequence)

    def receive(writer,predicate):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            assert writer.output.receiver.poll(max(0,deadline-time.monotonic()))
            value=writer.output.take()
            assert value['kind']!='fatal',value
            if value['kind']=='snapshot' and predicate(value['value']):
                return value['value']
        pytest.fail('prepared writer did not publish the observable state')

    publish(1)
    writer=PreparedWriter(channel.homes,channel.path,None,quota)
    writer.set_aggregate('mismatch-archive-fixture')
    try:
        first=receive(writer,lambda value:value.get('mismatch_history',{}).get('status')=='retry_pending')
        assert first['mismatch_history']['pending']==1
        assert first['sessions']
        source['title']='updated while archive unavailable'
        publish(2)
        updated=receive(writer,lambda value:any(row.get('title')==source['title'] for row in value.get('sessions',())))
        assert updated['mismatch_history']['error']
        writer.freeze()
        blocked.rmdir()
        saved=receive(writer,lambda value:value.get('mismatch_history',{}).get('status')=='ready')
        assert saved['mismatch_history']['revision']>0
        assert len(records(blocked))==1
    finally:
        writer.close()
        channel.close()


def test_saved_effort_and_modes_use_only_their_own_confirmed_evidence(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    row=call(requested_effort='high',requested_effort_source='wire',configured_effort='medium',
             requested_service_tier='priority',service_tier_source='wire',
             response_effort='low',response_effort_source='wire',response_effort_confirmed=True,
             response_service_tier='default',response_tier_confirmed=True,
             request_observed_at=99.,completed_observed_at=101.)
    source=Source(row)
    with closing(MismatchArchive(path)) as writer:
        capture(writer,source)
        saved=records(path)[0]
        assert saved['requested_effort']=='high' and saved['configured_effort']=='medium'
        assert saved['response_effort']=='low' and saved['response_service_tier']=='default'
        assert saved['request_observed_at']==99. and saved['completed_observed_at']==101.
        row.update(response_effort_confirmed=False,response_tier_confirmed=False)
        capture(writer,source)
        unknown=records(path)[0]
        assert unknown['response_effort'] is None and unknown['response_service_tier'] is None
        row.update(requested_effort_conflict=True,response_effort_conflict=True)
        capture(writer,source)
        conflicted=records(path)[0]
        assert conflicted['requested_effort'] is None and conflicted['effort'] is None
        assert conflicted['correction_at'] is None

def test_different_owner_conflict_corrects_saved_identity_and_deletion_restores_it(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    source=Source(call(sid='a'))
    with closing(MismatchArchive(path)) as writer:
        capture(writer,source)
        other=call(sid='b')
        other['model_alert_confirmed']=False
        source.rows.append(other)
        capture(writer,source,owners=frozenset({('home','b')}))
        corrected=records(path)[0]
        assert corrected['sid']=='a' and corrected['correction_reason']=='conflicting_occurrences'
    with closing(MismatchArchive(path)) as writer:
        capture(writer,source)
        source.rows.pop()
        capture(writer,source,owners=frozenset({('home','b')}))
        restored=records(path)[0]
        assert restored['sid']=='a' and restored['correction_at'] is None
        assert restored['first_captured_at']==corrected['first_captured_at']


def test_full_reconciliation_yields_and_resumes_before_hydrating_all_calls(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    source=Source(*(call(f'resp-{n}') for n in range(20)))
    with closing(MismatchArchive(path)) as writer:
        writer.capture(source,limit=2)
        assert writer.has_work
        assert 0<len(records(path))<=2
        while writer.has_work:
            writer.step(limit=2)
        assert len(records(path))==20
        assert writer.health()['status']=='ready'


def test_close_flushes_already_queued_snapshot_after_storage_recovers(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    holder=MismatchArchive(path)
    holder.retry(force=True)
    stopped=[False]
    writer=MismatchArchive(path,cancelled=lambda:stopped[0])
    try:
        capture(writer,Source(call()))
        assert writer.health()['pending']==1
        holder.close()
        stopped[0]=True
        writer.close()
        assert records(path)[0]['response_model']=='gpt-6-sol'
    finally:
        holder.close()
        writer.close()


def test_pinned_source_backlog_is_bounded_and_releases_rejected_and_closed_versions(tmp_path):
    released=[]
    writer=MismatchArchive(tmp_path/'usage.mismatches.sqlite')
    try:
        for name in ('first','second','overflow'):
            writer.capture(Source(call(name)),limit=0,release=lambda name=name:released.append(name))
        assert writer.health()['gap']
        assert released==['overflow']
        writer.close()
        assert released==['overflow','first','second']
    finally:
        writer.close()


def test_shutdown_deadline_and_overflow_gaps_remain_visible_after_restart(tmp_path):
    import time
    path=tmp_path/'usage.mismatches.sqlite'
    writer=MismatchArchive(path)
    writer.capture(Source(*(call(f'resp-{number}') for number in range(20))),limit=1)
    writer.close(deadline=time.monotonic())
    with closing(MismatchReader(path)) as reader:
        page=reader.window()
        assert page['capture_gap']
        assert len(page['window']['rows'])<20
    with closing(MismatchArchive(path)) as resumed:
        resumed.retry(force=True)
        assert resumed.health()['gap'] and resumed.health()['status']=='capture_gap'
        capture(resumed,Source())
        assert resumed.health()['gap']


def test_overflow_gap_is_committed_before_archive_writer_exits(tmp_path):
    path=tmp_path/'usage.mismatches.sqlite'
    with closing(MismatchArchive(path)) as writer:
        for number in range(3):writer.capture(Source(call(str(number))),limit=0)
        with closing(MismatchReader(path)) as reader:
            assert reader.window()['capture_gap']
