from contextlib import closing
from copy import deepcopy
import time

import pytest

from cachemonitor.analysis_delivery import SnapshotReceiver
from cachemonitor.prepared_runtime import PreparedWriter
from cachemonitor.usage_collection import CollectionChannel,empty_snapshot


def frame(sequence, *, epoch='delivery', reset=False, more=False, **values):
    return dict(protocol=2,epoch=epoch,sequence=sequence,reset=reset,more=more,
        prepared_token=('prepared',1),data_revision=1,call_revision=1,
        sessions=[],overlay_sessions=[],removed=[],model_candidates=[],cache_candidates=[],**values)


def test_receiver_commits_complete_continuation_without_exposing_partial_results():
    receiver = SnapshotReceiver()
    first = frame(1,reset=True,more=True,ts=100,
        filter_choices=dict(models=['gpt-6-astra']),models=['gpt-6-astra'])
    first['sessions'] = [dict(home='home',id='selected',title='Selected')]
    first['model_candidates'] = [dict(home='home',sid='selected',key='first')]
    assert receiver.receive(first) is None
    last = frame(2,filter_choices=dict(models=['gpt-6-sol'],efforts=['high']),models=['gpt-6-sol'])
    last['overlay_sessions'] = [dict(home='home',id='selected',calls=1)]
    last['model_candidates'] = [dict(home='home',sid='selected',key='second')]
    delivered = receiver.receive(last)
    assert delivered['ts'] == 100
    assert delivered['sessions'] == first['sessions']
    assert [row['id'] for row in delivered['overlay_sessions']] == ['selected']
    assert delivered['filter_choices'] == dict(models=['gpt-6-astra','gpt-6-sol'],efforts=['high'])
    assert delivered['models'] == ['gpt-6-astra','gpt-6-sol']
    assert [row['key'] for row in delivered['model_candidates']] == ['first','second']
    assert delivered['overlay_sessions'][0]['_prepared_token'] == ('prepared',1)


def test_receiver_discards_abandoned_continuation_on_a_new_epoch():
    receiver = SnapshotReceiver()
    abandoned = frame(1,reset=True,more=True)
    abandoned['sessions'] = [dict(home='home',id='abandoned')]
    assert receiver.receive(abandoned) is None
    replacement = frame(1,epoch='replacement',reset=True)
    replacement['sessions'] = [dict(home='home',id='current')]
    delivered = receiver.receive(replacement)
    assert [row['id'] for row in delivered['sessions']] == ['current']
    assert receiver.receive(frame(2)) is None


def test_receiver_keeps_last_complete_result_after_a_continuation_gap():
    receiver = SnapshotReceiver()
    initial = frame(1,reset=True)
    initial['sessions'] = [dict(home='home',id='confirmed')]
    delivered = receiver.receive(initial)
    pending = frame(2,more=True)
    pending['sessions'] = [dict(home='home',id='pending')]
    assert receiver.receive(pending) is None
    with pytest.raises(ValueError,match='누락'):
        receiver.receive(frame(4))
    assert [row['id'] for row in delivered['sessions']] == ['confirmed']
    assert list(receiver.sessions) == [('home','confirmed')]


def test_writer_delivers_selected_family_without_retaining_unrelated_metadata(tmp_path,monkeypatch):
    from cachemonitor.prepared_delivery import PreparedPublisher
    from test_data_contract import source

    monkeypatch.setattr(PreparedPublisher,'STATE_BYTES',256*1024)
    with closing(CollectionChannel([tmp_path/'home'],tmp_path/'source.sqlite')) as channel:
        channel.start_live()
        home = channel.homes[0]
        sessions = []
        for number in range(18):
            value = deepcopy(source())
            sid = 'selected' if number==0 else f'unrelated-{number:02d}'
            value.update(home=home,id=sid,title='Title '+('x'*8192))
            for row in value['history']:
                row.update(key=sid+':'+row['key'],effort='high' if number==0 else 'low')
            sessions.append(value)
        now = time.time()
        snapshot = empty_snapshot(channel.homes,channel.path,now)
        snapshot.update(sessions=sessions,usage_collection_complete=True,catalog_complete=True,
            last_usage_collection_success=now,request_activity=[],quota_by_home={},
            demand=dict(selected=[[home,'selected']],aggregate=True,evidence=[]),
            session_catalog=[[home,value['id'],None] for value in sessions],
            demand_members=[[home,value['id']] for value in sessions])
        snapshot['index'].update(loading=False,usage_complete=True)
        channel.publish(snapshot,'delivery-capacity',1)
        receiver = SnapshotReceiver()
        with closing(PreparedWriter(channel.homes,channel.path,None,tmp_path/'quota.sqlite')) as writer:
            writer.set_selected([(home,'selected')])
            writer.set_aggregate('complete-choices')
            deadline = time.monotonic()+10
            while True:
                assert writer.output.receiver.poll(max(0,deadline-time.monotonic())),'selected publication did not complete'
                message = writer.output.take()
                assert message['kind'] not in ('error','fatal'),message
                if message['kind'] != 'snapshot':
                    continue
                value = receiver.receive(message['value'])
                if value is not None and value.get('aggregate_request')=='complete-choices':
                    break
                assert time.monotonic() < deadline
            assert [row['id'] for row in value['sessions']] == ['selected']
            assert [row['id'] for row in value['overlay_sessions']] == ['selected']
            assert set(value['filter_choices']['efforts']) == {'high','low'}
            assert writer.thread.is_alive()
