import copy
import json
import sqlite3

from cachemonitor.analytics import analyze, overview_view, project_choices
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.analysis_delivery import SnapshotPublisher
from cachemonitor.codex_names import CodexNames
from cachemonitor.core import Session
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


def test_projectless_and_legacy_title_guards(tmp_path):
    home,path,state=named_home(tmp_path)
    state['thread-project-assignments']={}
    state['projectless-thread-ids']=[TID]
    (home/'.codex-global-state.json').write_text(json.dumps(state),encoding='utf-8')
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('update threads set name=NULL,title=?,first_user_message=?',('PRIVATE_PROMPT','PRIVATE_PROMPT and more private instructions'))
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        session=finish(index)['sessions'][0]
        assert session['title']==f'작업 {TID[:8]}·{TID[-6:]}'
        assert session['project_name']=='프로젝트 없는 작업'
        assert session['project'].startswith('projectless:')
        with sqlite3.connect(home/'state_5.sqlite') as db:
            sibling=CodexNames(home,db).resolve({'id':'child-task','cwd':r'C:\projects\app'})
        # The named workspace remains a project for unrelated tasks; only the
        # explicitly detached task belongs to the projectless group.
        assert sibling['project_name']=='구김평가'
        assert 'PRIVATE_PROMPT' not in ''.join(r[0] for r in index.db.execute('select data from metadata'))
        with sqlite3.connect(home/'state_5.sqlite') as db:
            db.execute('update threads set title=?',('명시적으로 변경한 이전 작업명',))
        renamed=finish(index,10021)['sessions'][0]
        assert renamed['title']=='명시적으로 변경한 이전 작업명'
    finally:
        index.close()


def test_workspace_hint_uses_app_name_and_db_assignment_wins(tmp_path):
    home,path,state=named_home(tmp_path)
    state['thread-project-assignments']={}
    state['thread-workspace-root-hints']={TID:r'C:\projects\app'}
    (home/'.codex-global-state.json').write_text(json.dumps(state),encoding='utf-8')
    with sqlite3.connect(home/'state_5.sqlite') as db:
        names=CodexNames(home,db)
        meta={'id':TID,'cwd':r'C:\Codex\worktrees\random\app','name':'worktree task'}
        resolved=names.resolve(meta)
        assert resolved['project_name']=='구김평가' and resolved['project_source']=='workspace_root'
        db.execute('insert into projects values(?,?,?)',('different','다른 프로젝트','{}'))
        names=CodexNames(home,db)
        resolved=names.resolve(dict(meta,project_id='different'))
        assert resolved['project_name']=='다른 프로젝트' and resolved['project_source']=='app_server'


def test_projectless_child_workspace_is_grouped_without_path_slug(tmp_path):
    home,path,state=named_home(tmp_path)
    state['local-projects']={}
    state['thread-project-assignments']={}
    state['projectless-thread-ids']=[TID]
    (home/'.codex-global-state.json').write_text(json.dumps(state),encoding='utf-8')
    with sqlite3.connect(home/'state_5.sqlite') as db:
        names=CodexNames(home,db)
        parent=names.resolve({'id':TID,'cwd':r'C:\projects\app'})
        child=names.resolve({'id':'child','cwd':r'C:\projects\app'})
        assert child['project']==parent['project']
        assert child['project_name']=='프로젝트 없는 작업'


def test_unassigned_scratch_folders_use_task_identity_and_agent_names(tmp_path):
    home,path,state=named_home(tmp_path)
    with sqlite3.connect(home/'state_5.sqlite') as db:
        names=CodexNames(home,db)
        first=names.resolve({'id':'child-one','cwd':r'C:\temp\opaque-long-slug','agent_nickname':'Lorentz'})
        second=names.resolve({'id':'child-two','cwd':r'C:\temp\other-slug','name':'Codex 작업명'})
        assert first['project']==second['project']
        assert first['project_name']=='기타 작업'
        assert first['display_title']=='Lorentz' and second['display_title']=='Codex 작업명'


def test_log_only_absent_task_and_old_metadata_do_not_duplicate_projectless_label(tmp_path):
    home,path,state=named_home(tmp_path)
    state['thread-project-assignments']={}
    state['projectless-thread-ids']=[TID]
    (home/'.codex-global-state.json').write_text(json.dumps(state),encoding='utf-8')
    orphan='87654321-abcd-abcd-abcd-222222222222'
    with sqlite3.connect(home/'logs_2.sqlite') as db:
        db.execute('insert into logs values(2,10000,0,?,?,?,?)',
                   ('feedback_tags',orphan,'process',
                    'request{transport="responses_websocket" api.path="/responses"}: auth_header_attached=true'))
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        snapshot=finish(index)
        log_session=next(s for s in snapshot['sessions'] if s['id']==orphan)
        assert log_session['cwd']=='' and log_session['project_name']=='기타 작업'
        assert (str(home),orphan) not in index.metadata
        choices=project_choices(snapshot['sessions'])
        assert set(choices['project_labels'].values())=={'프로젝트 없는 작업','기타 작업'}
        # Previously cached task metadata can outlive its app-server row. Resolve
        # its presentation without rereading or reclassifying recorded calls.
        retained='87654321-abcd-abcd-abcd-333333333333'
        old=Session(retained,str(home),title='이전 작업')
        old.add_usage(10000,'old-response',{'input_tokens':10,'cached_input_tokens':0,'output_tokens':1},'m')
        key=(str(home),retained)
        index.monitor.sessions[key]=old
        index.metadata[key]={'id':retained,'name':'이전 작업','cwd':''}
        before_bytes=index.bytes_read
        refreshed=finish(index,10011)
        cached=next(s for s in refreshed['sessions'] if s['id']==retained)
        assert cached['project']==log_session['project'] and cached['project_name']=='기타 작업'
        assert cached['title']=='이전 작업'
        assert len(cached['history'])==1 and cached['history'][0]['key']=='old-response'
        assert index.bytes_read==before_bytes
        assert set(project_choices(refreshed['sessions'])['project_labels'].values())=={'프로젝트 없는 작업','기타 작업'}
    finally:index.close()


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
