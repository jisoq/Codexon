from copy import deepcopy

import pytest

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session
from cachemonitor.dashboard_views import explorer
from cachemonitor.history_timeline_data import project_timeline


TIMELINE_FIELDS = {
    'home', 'sid', 'key', 'turn', 'ordinal', 'ts', 'model',
    'cost', 'input', 'output', 'cache_ratio', 'output_speed',
    'kind', 'duration', 'calls', 'total_calls', 'complete', 'state', 'partial',
}


def session_source(sid='selected', count=6):
    session = Session(sid, 'fixture', title=sid)
    session.cwd = 'V:/work/project'
    for index in range(count):
        turn = 'first' if index < count // 2 else 'second'
        session.add_usage(100 + index, f'record:{sid}:{index}', dict(
            input_tokens=1000, cached_input_tokens=0 if index % 2 else 800,
            output_tokens=100 + index), 'gpt-6-astra', turn, 'high', service_tier='Standard')
    source = session.view(100 + count)
    source['turn_records'] = {
        'first': dict(started_at=99, ended_at=99 + count // 2, state='완료'),
        'second': dict(started_at=99 + count // 2, ended_at=100 + count, state='완료'),
    }
    source.update(collection_complete=True, archived=False)
    return source


def query(**selection):
    return dict(page=2, presentation=True, start=0, end=1000, now=1000,
                period='all', model='', source='', archived=True,
                unit='response', method='mean', metric='cost', band=None, granularity='auto',
                explorer=dict(view='requests', session=('fixture', 'selected'),
                              timeline=True, sort='time_desc', **selection))


def engine_with(*sources):
    engine = AnalysisEngine()
    engine.ingest(sources or [session_source()])
    return engine


def test_projection_keeps_chronology_duplicates_and_missing_values_without_raw_fields():
    original = [
        dict(home='h', sid='s', key='later', ts=30, session_ordinal=3,
             input=100, cached=25, output=0, cost=0, output_speed=0, raw={'secret': 'record'}),
        dict(home='h', sid='s', key='first', ts=10, session_ordinal=1,
             input=100, cached=150, output=None, cost=None, output_speed=float('nan')),
        dict(home='h', sid='s', key='same-time', ts=10, session_ordinal=2,
             input=100, cached=0, output=-1, cost=True, output_speed=float('inf')),
        dict(key='no-time', ts=None), dict(key='infinite-time', ts=float('inf')),
        dict(key='boolean-time', ts=True), dict(key='text-time', ts='40'),
    ]
    result = project_timeline(original)
    assert result['total'] == 7 and result['omitted_timestamps'] == 4
    assert [row['key'] for row in result['rows'][:3]] == ['first', 'same-time', 'later']
    assert all(row['ts'] is None for row in result['rows'][3:])
    first, same, later = result['rows'][:3]
    assert first['cache_ratio'] is None and first['cost'] is None and first['output_speed'] is None
    assert same['cache_ratio'] == 0 and same['cost'] is None and same['output'] is None
    assert (later['cache_ratio'], later['output'], later['cost'], later['output_speed']) == (25, 0, 0, 0)
    assert all(set(row) == TIMELINE_FIELDS for row in result['rows'])
    assert original[0]['raw'] == {'secret': 'record'} and 'ordinal' not in original[0]


@pytest.mark.parametrize('input_tokens,cached', [(0, 0), (100, None), (100, -1), (100, True), (100.0, 20)])
def test_projection_uses_cache_token_validity(input_tokens, cached):
    row = project_timeline([dict(ts=1, input=input_tokens, cached=cached, rate=95)])['rows'][0]
    assert row['cache_ratio'] is None


@pytest.mark.parametrize('stamp', [0, 0.0, -1, 1e300])
def test_projection_preserves_untimed_calls_for_ordinal_axis(stamp):
    result = project_timeline([dict(key='unknown-time', ts=stamp, cost=0)])
    assert result['total'] == 1 and result['omitted_timestamps'] == 1
    assert result['rows'][0]['key'] == 'unknown-time' and result['rows'][0]['ts'] is None


def test_timeline_covers_whole_selected_session_independently_of_table_window_and_sort():
    engine = engine_with(session_source(count=170), session_source('other', count=3))
    request = query()
    request['explorer']['view'] = 'calls'
    request['table_windows'] = {'records': {'start': 80, 'size': 64}}
    first = engine.page_query(request)['explorer']
    assert first['records']['total'] == 170 and len(first['records']['rows']) == 64
    assert first['timeline']['total'] == 170 and len(first['timeline']['rows']) == 170
    assert [row['ordinal'] for row in first['timeline']['rows']] == list(range(1, 171))
    assert {row['sid'] for row in first['timeline']['rows']} == {'selected'}
    assert all(set(row) == TIMELINE_FIELDS for row in first['timeline']['rows'])
    assert '_call_identities' not in first
    before = dict(engine.metrics)
    request['explorer']['sort'] = 'cost_desc'
    request['table_windows']['records']['start'] = 0
    second = engine.page_query(request)['explorer']
    assert second['timeline'] == first['timeline']
    assert engine.metrics['priced_calls'] == before['priced_calls']
    assert engine.metrics['part_builds'] == before['part_builds']


def test_timeline_respects_filters_turn_search_and_outside_scope():
    engine = engine_with()
    request = query(filters=['cache_zero'], turn='first')
    request['explorer']['view'] = 'calls'
    data = engine.page_query(request)['explorer']
    assert [row['ordinal'] for row in data['timeline']['rows']] == [2]
    request['explorer']['outside'] = True
    data = engine.page_query(request)['explorer']
    assert [row['ordinal'] for row in data['timeline']['rows']] == [1, 2, 3]
    assert {row['key'] for row in data['timeline']['rows']} == {row['key'] for row in data['records']['rows']}
    excluded=engine.page_query({**request,'home':'not-selected'})['explorer']
    assert excluded['timeline']['rows']==[]
    request['explorer'].update(outside=False, search='no matching session')
    assert engine.page_query(request)['explorer']['timeline']['rows'] == []


def test_timeline_respects_selected_event_calls():
    engine = engine_with()
    request = query(event='event')
    request['explorer']['view'] = 'calls'
    analysis_query = {key: value for key, value in request.items() if key not in ('explorer', 'presentation')}
    result = deepcopy(engine.query(analysis_query))
    result['analysis']['sessions'][0]['cache_health'] = dict(events=[dict(
        id='event', baseline_keys=['record:selected:1'], occurrence_keys=['record:selected:3'], recovery_keys=[])])
    data = explorer(engine, result, request['explorer'])
    assert [row['ordinal'] for row in data['timeline']['rows']] == [2, 4]


def test_request_view_detail_membership_survives_cached_projection_and_filter_changes():
    engine = engine_with()
    request = query()
    first = engine.page_query(request)['explorer']
    assert first['detail'] is None
    request['explorer']['call_identity'] = ('fixture', 'selected', 'record:selected:2')
    selected = engine.page_query(request)['explorer']
    assert selected['detail']['key'] == 'record:selected:2' and selected['detail_in_scope']
    request['explorer']['call_identity'] = ('fixture', 'selected', 'record:selected:3')
    assert engine.page_query(request)['explorer']['detail_in_scope']
    request['explorer'].update(filters=['cache_zero'], call_identity=('fixture', 'selected', 'record:selected:2'))
    assert not engine.page_query(request)['explorer']['detail_in_scope']


def test_timeline_projection_runs_only_for_requested_selected_session(monkeypatch):
    engine = engine_with()
    def unexpected_projection(rows, kind='calls'):
        pytest.fail('Timeline projection ran for a table or project view')
    monkeypatch.setattr('cachemonitor.dashboard_views.project_timeline', unexpected_projection)
    request = query()
    request['explorer']['timeline'] = False
    assert engine.page_query(request)['explorer']['timeline'] is None
    request['explorer'].update(timeline=True, view='sessions', session=None)
    assert engine.page_query(request)['explorer']['timeline'] is None


def test_request_projection_retains_pending_and_partial_rows_without_invented_measurements():
    rows = [
        dict(home='h', sid='s', turn='complete', ordinal=1, ts=10, input=2000, output=120,
             cost=.04, rate=75, output_speed=40, responses=2, total_responses=2,
             complete=True, duration=12, state='완료'),
        dict(home='h', sid='s', turn='partial', ordinal=3, ts=None, input=None, output=0,
             cost=None, rate=None, responses=1, total_responses=4, complete=False,
             duration=30, state='조건 일부'),
        dict(home='h', sid='s', turn='waiting', ordinal=4, ts=0, responses=0,
             total_responses=0, complete=False, state='진행'),
    ]
    result = project_timeline(rows, kind='requests')
    complete, partial, waiting = result['rows']
    assert result['total'] == 3 and result['omitted_timestamps'] == 2
    assert [row['ordinal'] for row in result['rows']] == [1, 3, 4]
    assert (complete['key'], complete['calls'], complete['total_calls']) == ('complete', 2, 2)
    assert complete['duration'] == 12 and complete['cache_ratio'] == 75 and not complete['partial']
    assert all(row['kind'] == 'requests' and row['output_speed'] is None for row in result['rows'])
    assert partial['duration'] is None and partial['partial'] and partial['cost'] is None
    assert partial['input'] is None and partial['output'] == 0 and partial['ts'] is None
    assert waiting['calls'] == 0 and waiting['input'] is None and waiting['state'] == '진행'
    assert all(set(row) == TIMELINE_FIELDS for row in result['rows'])


@pytest.mark.parametrize('observations,expected', [
    ({'response_status': 'completed', 'completion_latency_ms': 1250}, 1.25),
    ({'response_status': 'completed', 'completion_latency_ms': 0}, 0),
    ({'response_status': 'completed', 'completion_latency_ms': 1250, 'timing_valid': False}, None),
    ({'response_status': 'completed', 'completion_latency_ms': 1250, 'observation_missing': True}, None),
    ({'response_status': 'completed', 'completion_latency_ms': 1250, 'model_conflict': True}, None),
    ({'response_status': 'failed', 'completion_latency_ms': 1250}, None),
    ({'response_status': 'completed', 'completion_latency_ms': -1}, None),
])
def test_call_projection_uses_valid_end_to_end_duration(observations, expected):
    row = project_timeline([dict(ts=10, session_ordinal=8, **observations)])['rows'][0]
    assert row['duration'] == expected and row['ordinal'] == 8


def test_request_timeline_matches_full_request_population_before_table_window():
    source = session_source(count=70)
    for index, row in enumerate(source['history']):
        row['turn'] = f'request-{index}'
    source['turn_records'] = {f'request-{index}': dict(
        started_at=99 + index, ended_at=100 + index, state='완료') for index in range(70)}
    engine = engine_with(source, session_source('other'))
    request = query()
    request['table_windows'] = {'records': {'start': 20, 'size': 64}}
    data = engine.page_query(request)['explorer']
    assert data['records']['total'] == 70 and len(data['records']['rows']) == 50
    assert data['timeline']['total'] == 70 and len(data['timeline']['rows']) == 70
    assert [row['ordinal'] for row in data['timeline']['rows']] == list(range(1, 71))
    chart = {row['turn']: row for row in data['timeline']['rows']}
    for row in data['records']['rows']:
        point = chart[row['turn']]
        assert point['ordinal'] == row['ordinal'] and point['kind'] == 'requests'
        assert all(point[key] == row[key] for key in ('cost', 'input', 'output', 'duration'))
        assert point['calls'] == row['responses'] and point['total_calls'] == row['total_responses']
    request['explorer']['sort'] = 'cost_desc'
    assert engine.page_query(request)['explorer']['timeline'] == data['timeline']


def test_filtered_request_timeline_preserves_partial_scope_missing_totals_and_ordinals():
    source = session_source()
    source['history'][3]['output'] = None
    engine = engine_with(source)
    request = query(filters=['cache_zero'])
    data = engine.page_query(request)['explorer']
    first, second = data['timeline']['rows']
    assert [row['ordinal'] for row in data['timeline']['rows']] == [1, 2]
    assert (first['calls'], first['total_calls'], first['input'], first['output']) == (1, 3, 1000, 101)
    assert (second['calls'], second['total_calls'], second['input'], second['output']) == (2, 3, 2000, None)
    assert second['cost'] is None
    assert all(row['partial'] and not row['complete'] and row['duration'] is None for row in data['timeline']['rows'])
    request['explorer']['turn'] = 'second'
    assert [row['ordinal'] for row in engine.page_query(request)['explorer']['timeline']['rows']] == [2]


def test_unlinked_request_is_a_separate_aggregate_and_parent_chart_keeps_own_calls():
    source = session_source()
    source['history'][0]['turn'] = None
    source['history'][1]['turn'] = None
    child = session_source('child')
    child['parent_thread_id'] = source['id']
    engine = engine_with(source, child)
    request = query(filters=['cache_zero'])
    data = engine.page_query(request)['explorer']
    rows = data['timeline']['rows']
    unlinked = next(row for row in rows if row['key'] == '__unlinked__')
    assert unlinked['ordinal'] is None and unlinked['turn'] == '__unlinked__'
    assert (unlinked['calls'], unlinked['total_calls'], unlinked['input'], unlinked['output']) == (1, 2, 1000, 101)
    assert unlinked['duration'] is None and unlinked['partial']
    assert {row['sid'] for row in rows} == {'selected'}
    assert sum(row['calls'] for row in rows) == 3
    assert data['group']['child_calls'] == 3


def test_overview_usage_totals_preserve_unknown_metrics_and_coverage_counts():
    source = session_source()
    source['history'][0]['output'] = None
    engine = engine_with(source)
    request = query();request['page'] = 0;request.pop('explorer')
    totals = engine.page_query(request)['overview']['usage_totals']
    assert totals == dict(calls=6, input=6000, output=None, input_known=6, output_known=5)
    request.update(start=500, end=600, period='custom')
    assert engine.page_query(request)['overview']['usage_totals'] == dict(
        calls=0, input=0, output=0, input_known=0, output_known=0)
