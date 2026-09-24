import copy
import json
import sqlite3

from cachemonitor.analytics import analyze, overview_view, project_choices
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.analysis_delivery import SnapshotPublisher
from cachemonitor.codex_names import CodexNames
from cachemonitor.index import UsageIndex
from test_core import TID, fixture_home
from test_index import finish
from test_performance import query


def named_home(tmp_path):
    home, path = fixture_home(tmp_path)
    with sqlite3.connect(home/'state_5.sqlite') as db:
        columns={r[1] for r in db.execute('pragma table_info(threads)')}
        for key in ('name', 'project_id', 'first_user_message'):
            if key not in columns:db.execute(f'alter table threads add column {key} TEXT')
        db.execute('create table projects(id TEXT PRIMARY KEY,name TEXT,metadata TEXT)')
        db.execute('insert into projects values(?,?,?)', ('server-project','구김평가','{}'))
        db.execute('update threads set name=?,cwd=?,title=?,first_user_message=?',
                   ('새 정책 모니터링 검토',r'C:\projects\app','PRIVATE_PROMPT_SENTINEL','PRIVATE_PROMPT_SENTINEL'))
    state = {'local-projects': {'legacy-project': {'name':'이전 이름','rootPaths':[r'C:\projects\app']}},
             'app-server-project-id-by-legacy-project-id-by-host': {f'local:{home}': {'legacy-project':'server-project'}},
             'thread-project-assignments': {TID: {'projectKind':'local','projectId':'legacy-project'}}}
    (home/'.codex-global-state.json').write_text(json.dumps(state),encoding='utf-8')
    return home,path,state


def test_actual_project_assignment_and_task_names_survive_snapshot_and_rename(tmp_path):
    home,path,state = named_home(tmp_path)
    index = UsageIndex([home],tmp_path/'index.sqlite')
    try:
        snapshot = finish(index)
        session = snapshot['sessions'][0]
        assert session['title']=='새 정책 모니터링 검토'
        assert session['project_name']=='구김평가'
        assert session['project_id']=='server-project'
        assert session['project_source']=='app_assignment'
        assert session['project'].endswith(':server-project')
        engine=AnalysisEngine();engine.ingest(snapshot['sessions'])
        rows=engine.query(query(page=0,start=0,end=10020))['analysis']['responses']
        assert rows[0]['project_name']=='구김평가'
        publisher=SnapshotPublisher()
        message=publisher.publish(snapshot,engine,snapshot['sessions'])
        assert message['filter_choices']['project_labels'][session['project']]=='구김평가'
        assert message['filter_choices']['transports']==['WebSocket']
        assert message['filter_choices']['transport_sources']==['log_time']
        before_revision=session['usage_revision']
        with sqlite3.connect(home/'state_5.sqlite') as db:
            db.execute('update threads set name=?',('이름 바꾼 작업',))
            db.execute('update projects set name=?',('새 프로젝트명',))
        renamed=finish(index,10021)['sessions'][0]
        assert renamed['usage_revision']>before_revision
        assert renamed['project']==session['project']
        assert renamed['title']=='이름 바꾼 작업' and renamed['project_name']=='새 프로젝트명'
        assert engine.ingest([renamed])
        after=engine.query(query(page=0,start=0,end=10030))
        assert after['analysis']['responses'][0]['title']=='이름 바꾼 작업'
        assert after['overview']['sources'][0]['label']=='새 프로젝트명'
        stored=''.join(row[0] for row in index.db.execute('select data from metadata'))
        assert 'PRIVATE_PROMPT_SENTINEL' not in stored
    finally:
        index.close()


def test_unassigned_scratch_folders_use_task_identity_and_agent_names(tmp_path):
    home,path,state=named_home(tmp_path)
    with sqlite3.connect(home/'state_5.sqlite') as db:
        names=CodexNames(home,db)
        first=names.resolve({'id':'child-one','cwd':r'C:\temp\opaque-long-slug','agent_nickname':'Lorentz'})
        second=names.resolve({'id':'child-two','cwd':r'C:\temp\other-slug','name':'Codex 작업명'})
        assert first['project']==second['project']
        assert first['project_name']=='기타 작업'
        assert first['display_title']=='Lorentz' and second['display_title']=='Codex 작업명'


def test_duplicate_project_names_do_not_merge_or_filter_each_other(tmp_path):
    home,path,state=named_home(tmp_path)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        first=finish(index)['sessions'][0]
        second=copy.deepcopy(first)
        second.update(id='other-task',project='project:scope:other-id',project_id='other-id',cwd=r'C:\other\app')
        for row in second['history']:row['key']='different-response'
        choices=project_choices([first,second])
        assert len(choices['projects'])==2
        assert len(set(choices['project_labels'].values()))==2
        assert all(name.startswith('구김평가 · ') for name in choices['project_labels'].values())
        analysis=analyze([first,second])
        overview=overview_view(analysis,0,10030)
        assert len(overview['sources'])==2 and len({row['label'] for row in overview['sources']})==2
        assert len(analyze([first,second],project=first['project'])['responses'])==1
        engine=AnalysisEngine();engine.ingest([first,second])
        result=engine.query(query(page=0,start=0,end=10030,project=second['project']))
        assert len(result['analysis']['responses'])==1
        assert result['analysis']['responses'][0]['sid']=='other-task'
    finally:
        index.close()
