"""Preview the source overlay using disposable settings and an owned window."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import zlib
from hashlib import sha256

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCENARIOS = ('multiagent', 'active', 'idle', 'sub-1', 'sub-2', 'sub-3', 'sub-4', 'sub-8', 'loading', 'error', 'mismatch', 'unknown', 'cache-miss', 'slow')


def set_preview_margin(margin):
    """Override geometry only inside this disposable preview process."""
    from cachemonitor import overlay_layout, overlay_monitor, overlay_view, overlay
    margin = max(12, min(24, int(margin)))
    right = overlay_layout.WIDTH - margin
    section = overlay_layout.WIDTH - 2 * margin
    actions = right - 72
    header_x = margin - 4
    header_width = actions - header_x
    values = dict(MARGIN=margin, HEADER_Y=margin-6, BOTTOM_MARGIN=margin-3,
                  RIGHT=right, SECTION_WIDTH=section, BODY_LEFT=margin, BODY_WIDTH=section,
                  ACTIONS_X=actions, HEADER_X=header_x, HEADER_WIDTH=header_width,
                  TITLE_WIDTH=header_width-8, POPUP_X=right-260, INLINE_CLOSE_X=actions,
                  INLINE_HEADER_WIDTH=header_width-8, TITLE_Y=margin-6)
    for module in (overlay_layout, overlay_monitor, overlay_view, overlay):
        for key, value in values.items():
            if hasattr(module, key):
                setattr(module, key, value)


def captured_snapshot(path, cancelled=lambda: False):
    """Read one consistent published collector checkpoint without registration."""
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f'Collector checkpoint does not exist: {path}')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        saved = db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        if saved is None:
            raise ValueError('Collector has no published checkpoint.')
        header = json.loads(zlib.decompress(saved[0]))
        schema = header.get('collection_schema', 1)
        if schema == 1:
            return header
        if schema != 2:
            raise ValueError(f'Unsupported collector checkpoint schema: {schema}')
        sessions = []
        for entry in header['session_manifest']:
            if cancelled():
                raise InterruptedError('Snapshot capture cancelled.')
            key = entry['home'], entry['sid']
            body = db.execute('SELECT revision,payload FROM sessions WHERE home=? AND sid=?', key).fetchone()
            if body is None or body[0] != entry['revision']:
                raise ValueError('Collector checkpoint has an inconsistent session revision.')
            digest = entry.get('body_digest')
            if digest is not None and sha256(body[1]).hexdigest() != digest:
                raise ValueError('Collector checkpoint body failed integrity verification.')
            data = json.loads(zlib.decompress(body[1]))
            if (data.get('home'), data.get('id')) != key:
                raise ValueError('Collector checkpoint session identity does not match its manifest.')
            sessions.append({**data, **entry['state']})
        return {key: value for key, value in header.items() if key not in ('collection_schema', 'session_manifest')} | {'sessions': sessions}


def synthetic_snapshot(scenario='multiagent', language='ko', new_calls=0):
    """Price explicit parent and child calls through production analysis."""
    from cachemonitor.core import Session
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    now = time.time()
    sources = []
    sub_count=int(scenario[4:]) if scenario.startswith('sub-') else 6 if scenario=='multiagent' else 0
    for agent in range(sub_count+1):
        sid = f'preview-agent-{agent}' if agent else 'preview-main'
        title = f'Worker {agent}' if agent else ('미리보기 작업' if language == 'ko' else 'Preview task')
        model = ('gpt-6-astra', 'gpt-6-sol', 'gpt-5.6-luna')[agent % 3]
        mode = 'Fast' if agent == 3 else 'Standard'
        session = Session(sid, 'synthetic-preview', title=title, model=model,
            parent_thread_id='preview-main' if agent else '', agent_nickname=f'Worker {agent}' if agent else '')
        for call in range(3 + agent if agent else 13):
            session.add_usage(now - 240 + call * 8 + agent, f'{sid}-{call}',
                dict(input_tokens=80000 + agent * 1000, cached_input_tokens=72000,
                     cache_write_input_tokens=0, output_tokens=2000 + agent * 200,
                     reasoning_output_tokens=500), model, f'{sid}-turn',
                ('high', 'medium', 'low')[agent % 3], mode)
        if scenario == 'multiagent':
            for call in range(new_calls):
                if (call + 1) % 7 == agent:
                    session.add_usage(now - new_calls + call, f'{sid}-next-{call}',
                        dict(input_tokens=80000 + agent * 1000, cached_input_tokens=72000,
                             cache_write_input_tokens=0, output_tokens=2000 + agent * 200,
                             reasoning_output_tokens=500), model, f'{sid}-turn',
                        ('high', 'medium', 'low')[agent % 3], mode)
        active = scenario == 'active' or sub_count>0 and agent in (0, 2, 5)
        session.running = active
        session.activity = now if active else now - 120
        session.request_state = dict(turn=f'{sid}-turn', state='진행' if active else '완료',
            started_at=now - 15, ended_at=None if active else now - 120, observed_at=now)
        data = session.view(now)
        data['collection_complete'] = True
        if agent:
            data['source'] = 'subagent'
        for i, row in enumerate(data['history']):
            speed = 35 if scenario == 'slow' and i >= 10 else 100 + agent * 5
            row.update(response_status='completed', timing_valid=True,
                completion_latency_ms=row['output'] / speed * 1000, transport='WebSocket',
                transport_source='response_id', requested_model=model, response_model=model, model_match='일치')
            if scenario == 'mismatch':
                row.update(response_model='gpt-6-sol', model_match='불일치', model_alert_confirmed=True, model_conflict=True)
            elif scenario == 'unknown':
                row.update(response_model=None, model_match='미확인')
            elif scenario == 'cache-miss':
                row.update(cached=0, cache_rate=0)
        sources.append(data)
    engine = AnalysisEngine()
    engine.ingest(sources)
    value = dict(ts=now, overlay_sessions=OverlaySummaries().collect(engine),
                 usage_collection_complete=True,index=dict(loading=False))
    if scenario == 'loading':
        value.update(overlay_sessions=[],usage_collection_complete=False,index=dict(loading=True))
    elif scenario == 'error':
        value.update(errors=['Synthetic preview collection failure'], collection_stale=True)
    return value


def verify(preview, output):
    """Exercise actual Quick controls using rendered geometry and deadlines."""
    from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt
    from PySide6.QtGui import QEnterEvent, QImage, QMouseEvent, QPainter, QPixmap
    from PySide6.QtTest import QTest
    from cachemonitor.overlay_chrome import named_item
    from cachemonitor.quick_qa import walk
    output.mkdir(parents=True, exist_ok=True)
    report = dict(source='synthetic', desktop='hidden', captures=[], checks=[])

    def wait(predicate, description):
        deadline = time.monotonic() + 5
        while True:
            preview.app.processEvents()
            if predicate():
                return
            if time.monotonic() >= deadline:
                raise AssertionError(f'Timed out: {description}')

    def rendered(window, name):
        def ready():
            item = named_item(window.quick.rootObject(), name)
            return item is not None and item.isVisible() and item.width() > 0 and item.height() > 0
        wait(ready, f'rendered {name}')
        return named_item(window.quick.rootObject(), name)

    def point(window, item, fraction=.5):
        position = item.mapToScene(QPointF(item.width() * fraction, item.height() / 2)).toPoint()
        assert window.quick.rect().contains(position), f'Control outside viewport: {item.objectName()}'
        return position

    def click(window, item):
        QTest.mouseClick(window.quick, Qt.LeftButton, pos=point(window, item))

    def hover(window, position):
        local = QPointF(position)
        global_pos = QPointF(window.quick.mapToGlobal(position))
        preview.app.sendEvent(window.quick, QEnterEvent(local, local, global_pos))
        preview.app.sendEvent(window.quick, QMouseEvent(QEvent.MouseMove, local, global_pos,
            Qt.NoButton, Qt.NoButton, Qt.NoModifier))

    def capture(name):
        c = preview.controller
        wait(lambda: c.widget.isVisible() and c.header.isVisible() and c.actions.isVisible(), 'visible overlay header')
        windows = [w for w in (c.widget, *c.chrome) if w.isVisible()]
        frames = {}
        def painted():
            for window in windows:
                if window.quick.grabFramebuffer().isNull():
                    return False
                frames[window] = window.grab()
            for window in (c.header, c.actions):
                pixels = frames[window].toImage().convertToFormat(QImage.Format_RGBA8888)
                if not any(bytes(pixels.bits())[3::4]):
                    return False
            return True
        wait(painted, 'painted frame including header and actions')
        bounds = windows[0].geometry()
        for window in windows[1:]:
            bounds = bounds.united(window.geometry())
        image = QPixmap(bounds.size())
        image.fill(Qt.transparent)
        painter = QPainter(image)
        for window in windows:
            painter.drawPixmap(window.pos() - bounds.topLeft(), frames[window])
        painter.end()
        assert image.save(str(output / f'{name}.png'))
        report['captures'].append(f'{name}.png')

    try:
        for theme in ('dark', 'light'):
            preview.theme.setCurrentText(theme)
            preview.scenario.setCurrentText('multiagent')
            preview.rebuild()
            c = preview.controller
            wait(lambda: c.widget.isVisible() and c.detail.isVisible(), 'multiagent overlay')
            assert c.widget.data['descendants'] == 6
            heartbeat = rendered(c.widget, 'overlayHeartbeat')
            dimensions = heartbeat.width(), heartbeat.height()
            wait(lambda: heartbeat.opacity() < .5, 'heartbeat fades')
            wait(lambda: heartbeat.opacity() > .85, 'heartbeat brightens')
            assert (heartbeat.width(), heartbeat.height()) == dimensions
            report['checks'].append(f'{theme}: heartbeat blinks without changing dimensions')
            agent_pulse = rendered(c.widget, 'agentHeartbeat-main')
            assert agent_pulse.property('visible')
            wait(lambda: agent_pulse.opacity() < .35, 'agent heartbeat fades subtly')
            wait(lambda: agent_pulse.opacity() > .55, 'agent heartbeat brightens subtly')
            model = c.widget.content_model
            agent_scroll = rendered(c.detail, 'detailScroll')
            for indicator in model.state['detailHeartbeats']:
                pulse = named_item(c.detail.quick.rootObject(), 'agentHeartbeat-' + indicator['id'])
                assert pulse is not None
                assert pulse.property('visible') == (indicator['mode'] == 'active')
                in_view = pulse.y() + pulse.height() > agent_scroll.property('contentY') and pulse.y() < agent_scroll.property('contentY') + agent_scroll.height()
                assert pulse.property('blinking') == (indicator['mode'] == 'active' and in_view)
            report['checks'].append(f'{theme}: individual activity indicators follow own requests and blink subtly')
            capture(f'{theme}-multiagent')
            chart=rendered(c.links,'nav-inspect-0-0')
            latest=model.latest_call();selection=model.selected_id
            hover(c.links,point(c.links,chart))
            wait(lambda:model.displayed_call()==model.rows()[0],'chart hover value')
            capture(f'{theme}-chart-hover')
            click(c.links,chart)
            assert model.selected_id==selection and not model.graph_pinned
            hover(c.links,QPoint(1,1))
            wait(lambda:model.displayed_call()==latest,'chart hover leave')
            report['checks'].append(f'{theme}: chart hover shows the inspected call; clicking never pins it')
            click(c.actions, rendered(c.actions, 'settingsButton'))
            wait(lambda: c.toolbar.isVisible(), 'settings popup')
            for limit in (2,4,3):
                click(c.toolbar,rendered(c.toolbar,f'agentCount-{limit}'))
                wait(lambda:c.widget.content_model.visible_agent_count()==limit,'visible agent count choice')
            report['checks'].append(f'{theme}: visible agent count buttons update the saved limit')
            capture(f'{theme}-height')
            click(c.actions, rendered(c.actions, 'settingsButton'))
            wait(lambda: not c.toolbar.isVisible(), 'settings close')
            main = rendered(c.links, 'nav-main-detail')
            pointer = next(child for child in main.childItems()
                           if child.metaObject().indexOfProperty('containsMouse') >= 0)
            hover(c.links, QPoint(c.links.quick.width()-1, 1))
            wait(lambda: not pointer.property('containsMouse'), 'main hover reset')
            main_rect = QRect(main.mapToScene(QPointF(0, 0)).toPoint(),
                              QSize(round(main.width()), round(main.height())))
            before_main_hover = c.links.quick.grabFramebuffer().copy(main_rect)
            hover(c.links, point(c.links, main))
            wait(lambda: pointer.property('containsMouse'), 'main hover')
            wait(lambda: c.links.quick.grabFramebuffer().copy(main_rect) != before_main_hover, 'painted main hover')
            capture(f'{theme}-main-hover')
            before = c.widget.content_model.main_expanded
            click(c.links, rendered(c.links, 'nav-main-detail'))
            wait(lambda: c.widget.content_model.main_expanded != before, 'main expansion')
            capture(f'{theme}-main-expanded')
            click(c.links, rendered(c.links, 'nav-main-detail'))
            wait(lambda: c.widget.content_model.main_expanded == before, 'main collapse')
            def visible_actions():
                return [item for item in walk(c.detail.quick.rootObject())
                    if item.objectName().startswith('detailAction-') and item.isVisible()
                    and c.detail.quick.rect().contains(item.mapToScene(QPointF(item.width()/2, item.height()/2)).toPoint())]
            wait(lambda: bool(visible_actions()), 'subagent row')
            item = visible_actions()[0]
            name, expanded = item.objectName(), bool(item.property('checked'))
            hover(c.detail, QPoint(c.detail.quick.width()-1, 1))
            wait(lambda: not item.property('hovered'), 'subagent hover reset')
            top = item.mapToScene(QPointF(0, 0)).toPoint()
            rect = QRect(top, QSize(round(item.width()), round(item.height())))
            before_hover = c.detail.quick.grabFramebuffer().copy(rect)
            hover(c.detail, point(c.detail, item))
            wait(lambda: bool(item.property('hovered')), 'subagent hover')
            wait(lambda: c.detail.quick.grabFramebuffer().copy(rect) != before_hover, 'painted subagent hover')
            capture(f'{theme}-hover')
            click(c.detail, item)
            wait(lambda: bool(rendered(c.detail, name).property('checked')) != expanded, 'subagent expansion')
            report['checks'].append(f'{theme}: painted main and subagent hover, main and subagent expansion')
            capture(f'{theme}-agent-expanded')
            scroll = rendered(c.detail, 'detailScroll')
            bar = rendered(c.detail, 'detailScrollBar')
            thumb = bar.property('contentItem')
            assert thumb.isVisible() and thumb.opacity() > 0
            previous_scroll = scroll.property('contentY')
            previous_position = bar.property('position')
            scroll.setProperty('contentY', scroll.property('contentHeight') - scroll.height())
            wait(lambda: bar.property('position') > previous_position, 'scrollbar tracks position')
            capture(f'{theme}-scrollbar')
            scroll.setProperty('contentY', previous_scroll)
            wait(lambda: abs(bar.property('position') - previous_position) < .001, 'scrollbar restores position')
            report['checks'].append(f'{theme}: idle scrollbar remains visible and tracks scroll position')
            for actor in range(1, 8):
                before_scroll = scroll.property('contentY')
                QTest.mouseClick(preview.next_call_button, Qt.LeftButton,
                                 pos=preview.next_call_button.rect().center())
                expected = f'preview-agent-{actor}' if actor < 7 else 'preview-main'
                wait(lambda: c.widget.content_model.latest_call().get('sid') == expected, 'newest actor selection')
                model = c.widget.content_model
                assert scroll.property('contentY') == before_scroll, 'A new call moved the subagent viewport'
                if actor in (1,7):
                    levels=[]
                    def flash_finished():
                        level=model.agent_highlight_opacity
                        if not levels or level!=levels[-1]:levels.append(level)
                        return len(levels)>2 and max(levels)>0 and level==0
                    wait(flash_finished,'latest agent flash fades away')
                    assert any(after<before for before,after in zip(levels,levels[1:]))
                    report['checks'].append(f'{theme}: latest agent flash brightens and fades to zero during live refresh')
                if actor < 7:
                    group = next(group for group in model.detail_groups() if group['id'] == expected)
                    assert (', fast' in model.subagent_call_label(group)) == (actor == 3)
                    assert model.detail_groups()[0]['id'] == expected, 'Newest subagent is not first'
                    def first_action_name():
                        return min((item for item in walk(c.detail.quick.rootObject())
                            if item.objectName().startswith('detailAction-')), key=lambda item: item.y()).objectName()
                    wait(lambda: first_action_name() == 'detailAction-group-' + model.detail_groups()[0]['key'],
                         'rendered newest subagent row')
                    if actor == 3:
                        capture(f'{theme}-fast-agent')
                assert all(link['id'] in ('main-detail', 'collection') or link['interaction']=='inspect' for link in model.monitor_links())
            report['checks'].append(f'{theme}: next-call control cycles every actor, newest-first rows, stable scroll and hover-only charts')
            capture(f'{theme}-next-call')
            click(c.actions, rendered(c.actions, 'settingsButton'))
            click(c.toolbar, rendered(c.toolbar, 'chartStyle-filled-line'))
            wait(lambda: c.chart_style == 'filled-line', 'filled-line chart')
            click(c.actions, rendered(c.actions, 'settingsButton'))
            capture(f'{theme}-filled-line')
            click(c.actions, rendered(c.actions, 'settingsButton'))
            click(c.toolbar, rendered(c.toolbar, 'chartStyle-bar'))
            wait(lambda: c.chart_style == 'bar', 'bar chart')
            click(c.actions, rendered(c.actions, 'settingsButton'))
            report['checks'].append(f'{theme}: chart style controls render bars and per-agent filled lines')
        preview.compact.setChecked(True)
        wait(lambda: preview.controller.widget.isVisible() or preview.controller.icon.isVisible(), 'compact host')
        capture('compact')
        report['checks'].append('compact host remains visible')
        for scenario in ('active', 'idle', 'loading', 'error', 'mismatch', 'unknown'):
            preview.scenario.setCurrentText(scenario)
            preview.rebuild()
            wait(lambda: preview.controller.widget.isVisible(), scenario)
            capture(scenario)
            if scenario in ('active', 'idle', 'unknown'):
                c=preview.controller
                pulse=named_item(c.widget.quick.rootObject(),'agentHeartbeat-main')
                assert pulse is not None
                assert pulse.property('visible') == (c.widget.content_model.state['mainHeartbeats'][0]['mode']=='active')
                report['checks'].append(f'{scenario}: main activity dot is only visible while active')
            if scenario == 'error':
                c=preview.controller
                status=rendered(c.widget,'overlayHeartbeatLabel')
                assert status.property('text')==c.widget.content_model.state['heartbeatLabel']
                assert not c.widget.quick.rootObject().findChild(QObject,'overlayHeartbeat').property('visible')
                assert not any(link.get('interaction')=='tooltip' for link in c.widget.content_model.monitor_links())
                report['checks'].append('error status replaces the heartbeat without hover')
        preview.compact.setChecked(False)
        preview.theme.setCurrentText('light')
        preview.controller.set_visible_agent_count(3)
        height_cases=[]
        for count in (0,1,2,3,4,8):
            preview.scenario.setCurrentText(f'sub-{count}' if count else 'idle')
            preview.rebuild()
            c=preview.controller;m=c.widget.content_model
            wait(lambda:c.widget.isVisible(),'height preview')
            if count:
                scroll=rendered(c.detail,'detailScroll')
                actions=[item for item in m.state['detailActions'] if item['id'].startswith('group-')]
                for action in actions[:3]:
                    item=rendered(c.detail,'detailAction-'+action['id'])
                    assert item.y()+item.height()<=scroll.height()+1
                assert (scroll.property('contentHeight')>scroll.height()+1)==(count>3)
            capture(f'height-sub-{count}')
            height_cases.append(dict(agents=count,height=m.base_height(),file=f'height-sub-{count}.png'))
            if count==0:
                click(c.actions,rendered(c.actions,'settingsButton'))
                wait(lambda:c.toolbar.isVisible(),'main-only settings')
                assert not named_item(c.toolbar.quick.rootObject(),'agentCount-1').isVisible()
                capture('height-main-settings')
                c.close_popup()
        assert height_cases[0]['height']<height_cases[1]['height']<height_cases[2]['height']<height_cases[3]['height']
        assert height_cases[3]['height']==height_cases[4]['height']==height_cases[5]['height']
        preview.scenario.setCurrentText('sub-4');preview.rebuild()
        c=preview.controller
        c.set_visible_agent_count(4)
        capture('height-limit-4')
        shown=c.widget.content_model.base_height()
        preview.scenario.setCurrentText('sub-8');preview.rebuild()
        c=preview.controller
        assert c.widget.content_model.visible_agent_count()==4 and c.widget.content_model.base_height()==shown
        capture('height-limit-4-growth')
        click(c.actions,rendered(c.actions,'settingsButton'))
        wait(lambda:c.toolbar.isVisible(),'visible agent count settings')
        capture('height-count-settings')
        for limit in (1,2,3,4):
            click(c.toolbar,rendered(c.toolbar,f'agentCount-{limit}'))
            wait(lambda:c.widget.content_model.visible_agent_count()==limit,'agent count choice')
            scroll=rendered(c.detail,'detailScroll')
            actions=[item for item in c.widget.content_model.state['detailActions'] if item['id'].startswith('group-')]
            last=rendered(c.detail,'detailAction-'+actions[limit-1]['id'])
            following=rendered(c.detail,'detailAction-'+actions[limit]['id'])
            assert last.y()+last.height()<=scroll.height()+1
            assert following.y()+following.height()>scroll.height()+1
            capture(f'height-count-{limit}')
        click(c.toolbar,rendered(c.toolbar,'agentCount-3'))
        wait(lambda:c.widget.content_model.visible_agent_count()==3,'default count selected')
        c.close_popup()
        assert c.widget.content_model.base_height()==height_cases[3]['height']
        capture('height-reset')
        padding_cases=[]
        for count in range(5):
            preview.scenario.setCurrentText(f'sub-{count}' if count else 'idle');preview.rebuild()
            c=preview.controller;m=c.widget.content_model
            if count:
                c.set_visible_agent_count(4)
                actions=[item for item in m.state['detailActions'] if item['id'].startswith('group-')]
                target_window=c.detail
                last=rendered(c.detail,'detailAction-'+actions[-1]['id'])
                scroll=rendered(c.detail,'detailScroll')
                assert scroll.property('contentHeight')<=scroll.height()+1
            else:
                target_window=c.links
                last=rendered(c.links,'nav-main-detail')
            capture(f'padding-{count}')
            end=target_window.quick.mapToGlobal(last.mapToScene(QPointF(0,last.height())).toPoint()).y()
            pixels=QImage(str(output/f'padding-{count}.png'))
            ink=[(x,y) for y in range(pixels.height()) for x in range(pixels.width())
                 if (color:=pixels.pixelColor(x,y)).alpha()>200 and max(color.red(),color.green(),color.blue())<120]
            insets=dict(top=min(y for x,y in ink),right=pixels.width()-1-max(x for x,y in ink),
                        bottom=pixels.height()-1-max(y for x,y in ink),left=min(x for x,y in ink))
            padding_cases.append(dict(agents=count,bottom_gap=c.widget.y()+c.widget.height()-end,
                                      visual_insets=insets,file=f'padding-{count}.png'))
            hover(target_window,point(target_window,last))
            pointer=last if count else next(child for child in last.childItems()
                if child.metaObject().indexOfProperty('containsMouse')>=0)
            wait(lambda:bool(pointer.property('hovered' if count else 'containsMouse')),'last agent hover')
            capture(f'padding-hover-{count}')
        gaps=[case['bottom_gap'] for case in padding_cases]
        assert max(gaps)-min(gaps)<=1
        report['bottom_padding_cases']=padding_cases
        report['checks'].append('rendered main-only and one to four subagent summary edges have consistent bottom padding within one logical pixel')
        report['height_cases']=height_cases
        report['checks'].append('automatic height fits the selected one to four agents; per-conversation selection survives growth and restart')
        report['checks'].append('main-only settings hide agent count controls')
        preview.margin.setFocus()
        for key, expected in ((Qt.Key_Home,12),(Qt.Key_End,24),(Qt.Key_Left,23)):
            QTest.keyClick(preview.margin,key)
            wait(lambda:preview.controller.widget.content_model.state.get('overlayMargin')==expected,
                 'preview margin slider updates rendered geometry')
            capture(f'margin-{expected}')
        preview.margin.setValue(preview.args.margin)
        wait(lambda:preview.controller.widget.content_model.state.get('overlayMargin')==preview.args.margin,
             'preview margin restored')
        report['checks'].append('preview margin slider updates geometry from 12 to 24 and supports one-pixel keyboard steps')
        cards=[('메인 상세: 토큰과 비용 표기','light-main-expanded.png'),('서브 상세: 토큰과 비용 표기','light-agent-expanded.png'),
               ('차트 호버: 비용 표기','light-hover.png'),
               ('메인 단독','height-sub-0.png'),('서브 1개','height-sub-1.png'),('서브 2개','height-sub-2.png'),
               ('서브 3개','height-sub-3.png'),('서브 4개: 3개 높이 유지','height-sub-4.png'),('서브 8개: 3개 높이 유지','height-sub-8.png'),
               ('4개 표시 선택','height-limit-4.png'),('서브 8개로 증가: 4개 높이 유지','height-limit-4-growth.png'),
               ('기본 3개 선택','height-reset.png'),('메인 단독 설정','height-main-settings.png'),('표시 개수 설정','height-count-settings.png'),
               *[(f'{limit}개 표시 설정',f'height-count-{limit}.png') for limit in (1,2,3,4)],
               *[(('메인 단독' if count==0 else f'서브 {count}개')+' 하단 여백',f'padding-{count}.png') for count in range(5)]]
        gallery=''.join(f'<figure><figcaption>{title}</figcaption><img src="{file}" alt="{title}"></figure>' for title,file in cards)
        (output/'height-preview.html').write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>오버레이 높이 미리보기</title><style>body{font:14px/1.5 system-ui,sans-serif;margin:24px;background:#f6f6f6;color:#222}h1{font-size:20px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px;align-items:start}figure{margin:0}figcaption{margin-bottom:10px;font-weight:600}img{max-width:100%;height:auto;vertical-align:top}</style><h1>오버레이 높이 미리보기</h1><p>실제 소스 오버레이의 합성 데이터 화면입니다. 설치본은 변경하지 않았습니다.</p><main>'+gallery+'</main></html>',encoding='utf-8')
        padding_cards=[]
        for case in padding_cases:
            count=case['agents'];name='메인 단독' if count==0 else f'서브 {count}개'
            padding_cards.append(f'<figure><figcaption>{name}</figcaption><a class="crop" href="padding-{count}.png"><img src="padding-{count}.png" alt="{name}"></a><p>호버 상태</p><a class="crop" href="padding-hover-{count}.png"><img src="padding-hover-{count}.png" alt="{name} 호버"></a></figure>')
        (output/'bottom-padding-preview.html').write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>오버레이 여백 비교</title><style>body{margin:24px;font:14px/1.6 system-ui,sans-serif;background:#f6f6f6;color:#222}h1{font-size:20px}main{display:grid;grid-template-columns:repeat(auto-fit,300px);gap:24px}figure{margin:0}figcaption{font-weight:600;margin-bottom:12px}.crop{display:block;position:relative;height:140px;overflow:hidden}.crop img{position:absolute;bottom:0;left:0;width:100%}a{color:inherit}</style><h1>메인 단독과 서브 1~4개 여백 비교</h1><p>기본 상태와 호버 상태의 하단을 같은 높이로 비교합니다. 이미지를 누르면 전체 화면을 확인할 수 있습니다.</p><main>'+''.join(padding_cards)+'</main><p><a href="height-preview.html">전체 상황별 미리보기</a></p></html>',encoding='utf-8')
        errors = [str(error) for w in (preview.controller.widget, *preview.controller.chrome) for error in w.qml_errors]
        assert not errors, errors
        report['passed'] = True
    except Exception as error:
        report.update(passed=False, error=str(error))
        raise
    finally:
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


class Preview:
    def __init__(self, app, args, settings):
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QComboBox, QCheckBox, QSlider
        from cachemonitor.overlay_windows import WindowsOverlay
        self.app, self.settings, self.controller = app, settings, None
        self.args, self.worker, self.snapshot = args, None, dict(overlay_sessions=[], index=dict(loading=True))
        self.capture_worker = None
        self.new_calls = 0
        self.synthetic_scenario = args.scenario
        self.selected = args.session or 'preview-main'
        self.host = QWidget()
        self.host.setWindowTitle('Codexon Overlay Preview - synthetic data')
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(20, 20, 420, 20)
        self.note = QLabel('설치 없이 소스 오버레이를 확인합니다.\nSynthetic data and temporary settings.')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.margin_label = QLabel(f'상하좌우 여백: {args.margin}px')
        layout.addWidget(self.margin_label)
        self.margin = QSlider(Qt.Horizontal)
        self.margin.setObjectName('previewMargin')
        self.margin.setAccessibleName('상하좌우 여백')
        self.margin.setRange(12, 24)
        self.margin.setSingleStep(1)
        self.margin.setTickPosition(QSlider.TicksBelow)
        self.margin.setTickInterval(1)
        self.margin.setValue(args.margin)
        layout.addWidget(self.margin)
        layout.addWidget(QLabel('12px                                      24px'))
        self.margin_timer = QTimer(self.host)
        self.margin_timer.setSingleShot(True)
        self.margin_timer.setInterval(60)
        self.margin_timer.timeout.connect(self.rebuild)
        self.margin.valueChanged.connect(self.change_margin)
        self.language, self.theme, self.scenario = QComboBox(), QComboBox(), QComboBox()
        for control, choices, selected in ((self.language, ('ko', 'en'), args.language),
                (self.theme, ('dark', 'light'), args.theme), (self.scenario, SCENARIOS, args.scenario)):
            control.addItems(choices)
            control.setCurrentText(selected)
            layout.addWidget(control)
            control.currentTextChanged.connect(self.rebuild)
        self.compact = QCheckBox('Compact host / 작은 창')
        self.compact.setChecked(args.compact)
        layout.addWidget(self.compact)
        self.compact.toggled.connect(self.resize_host)
        self.next_call_button = QPushButton('Next call / 다음 호출')
        self.next_call_button.setVisible(not args.snapshot)
        self.next_call_button.clicked.connect(self.next_call)
        layout.addWidget(self.next_call_button)
        self.session = QComboBox()
        self.session.setVisible(bool(args.snapshot))
        self.session.currentIndexChanged.connect(self.select_session)
        layout.addWidget(self.session)
        self.capture_button = QPushButton('Capture source again / 원본 다시 읽기')
        self.capture_button.setVisible(bool(args.snapshot))
        self.capture_button.clicked.connect(self.capture_source)
        layout.addWidget(self.capture_button)
        layout.addStretch()
        close = QPushButton('Close / 닫기')
        close.clicked.connect(self.host.close)
        layout.addWidget(close)
        self.native = WindowsOverlay()
        self.hwnd = int(self.host.winId())
        self.native.visible_target = lambda hwnd: hwnd == self.hwnd and bool(
            self.native.u.IsWindowVisible(hwnd) and not self.native.u.IsIconic(hwnd))
        self.resize_host()
        self.host.show()
        self.rebuild()
        self.timer = QTimer()
        self.timer.timeout.connect(self.poll)
        self.timer.start(500)
        app.aboutToQuit.connect(self.cleanup)
        if args.snapshot:
            self.scenario.setEnabled(False)
            self.capture_source()

    def resize_host(self):
        area = self.app.primaryScreen().availableGeometry()
        width, height = (900, 510) if self.compact.isChecked() else (1100, 900)
        self.host.resize(min(width, area.width()-40), min(height, area.height()-48))
        self.host.move(area.center()-self.host.rect().center())
        if self.controller:
            self.poll()

    def change_margin(self, value):
        self.margin_label.setText(f'상하좌우 여백: {value}px')
        self.margin_timer.start()

    def rebuild(self, *_):
        from cachemonitor.i18n import set_language
        from cachemonitor.overlay import OverlayController
        if self.controller:
            self.controller.stop()
        set_preview_margin(self.margin.value())
        set_language(self.language.currentText())
        self.settings.setValue('ui/theme', self.theme.currentText())
        if not self.args.snapshot:
            if self.synthetic_scenario != self.scenario.currentText():
                self.new_calls = 0
                self.synthetic_scenario = self.scenario.currentText()
            self.snapshot = synthetic_snapshot(self.scenario.currentText(), self.language.currentText(), self.new_calls)
        self.next_call_button.setEnabled(not self.args.snapshot and self.scenario.currentText() == 'multiagent')
        self.controller = OverlayController(self.settings, native_enabled=False)
        self.controller.native = self.native
        self.poll()

    def next_call(self):
        if self.args.snapshot or self.scenario.currentText() != 'multiagent':
            return
        self.new_calls += 1
        self.snapshot = synthetic_snapshot('multiagent', self.language.currentText(), self.new_calls)
        self.poll()

    def select_session(self, index):
        if index >= 0:
            self.selected = self.session.itemData(index)
            if self.controller:
                self.poll()

    def capture_source(self):
        from PySide6.QtCore import QThread, Signal
        if not self.capture_button.isEnabled():
            return
        self.stop_worker()
        self.capture_button.setEnabled(False)
        self.note.setText(f'Capturing source checkpoint in background:\n{self.args.snapshot}')
        class CaptureThread(QThread):
            captured = Signal(object)
            failed = Signal(str)

            def run(reader):
                try:
                    value = captured_snapshot(self.args.snapshot, reader.isInterruptionRequested)
                    if not reader.isInterruptionRequested():
                        reader.captured.emit(value)
                except Exception as error:
                    if not reader.isInterruptionRequested():
                        reader.failed.emit(str(error))
        self.capture_worker = CaptureThread(self.host)
        self.capture_worker.captured.connect(self.analyze_capture)
        self.capture_worker.failed.connect(self.capture_failed)
        self.capture_worker.start()

    def capture_failed(self, error):
        self.note.setText(f'Cannot capture source: {error}')
        self.capture_button.setEnabled(True)

    def analyze_capture(self, source):
        from cachemonitor.analysis_worker import AnalysisBridge
        stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(source.get('ts') or time.time()))
        self.note.setText(f'Captured source checkpoint: {stamp}\nStatic snapshot. Use capture again for newer data.')
        self.host.setWindowTitle('Codexon Overlay Preview - captured source snapshot')
        self.worker = AnalysisBridge([], static_snapshot=source,
            quota_path=Path(self.settings.fileName()).parent/'preview-quota.sqlite')
        self.worker.snapshot.connect(self.receive_capture)
        self.worker.failure.connect(self.capture_failed)
        self.worker.start()

    def receive_capture(self, value):
        from PySide6.QtCore import QSignalBlocker
        self.snapshot = value
        previous = self.selected
        with QSignalBlocker(self.session):
            self.session.clear()
            for session in sorted(value.get('overlay_sessions', []), key=lambda s: s.get('activity', 0), reverse=True):
                self.session.addItem(session.get('title') or session['id'], session['id'])
            index = self.session.findData(previous)
            self.session.setCurrentIndex(max(0, index))
        self.select_session(self.session.currentIndex())
        self.capture_button.setEnabled(True)

    def stop_worker(self):
        if self.worker:
            self.worker.snapshot.disconnect(self.receive_capture)
            self.worker.requestInterruption()
            self.worker.wait()
            self.worker = None

    def poll(self):
        from cachemonitor.overlay_tracking import Selection
        if not self.args.snapshot:
            self.snapshot['ts'] = time.time()
        self.controller.receive_snapshot(self.snapshot)
        self.controller.receive_target(dict(target=dict(hwnd=self.hwnd), selection=Selection(self.selected)))

    def cleanup(self):
        self.timer.stop()
        if self.capture_worker:
            self.capture_worker.requestInterruption()
            self.capture_worker.wait()
            self.capture_worker = None
        self.stop_worker()
        if self.controller:
            self.controller.stop()


def main():
    from cachemonitor.overlay_layout import MARGIN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=SCENARIOS, default='multiagent')
    parser.add_argument('--theme', choices=('dark', 'light'), default='dark')
    parser.add_argument('--language', choices=('ko', 'en'), default='ko')
    parser.add_argument('--compact', action='store_true')
    parser.add_argument('--margin', type=int, choices=range(12, 25), default=MARGIN,
                        help='Initial preview-only outer margin in pixels (12-24)')
    parser.add_argument('--snapshot', nargs='?', const='default', type=str,
                        help='Capture an existing collector checkpoint read-only; optionally supply its .collection.sqlite path')
    parser.add_argument('--session', help='Initially select this thread id in captured source mode')
    parser.add_argument('--verify', type=Path, metavar='OUTPUT', help='Verify on a hidden desktop and save captures')
    parser.add_argument('--verification-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.snapshot and args.verify:
        parser.error('--verify uses reproducible synthetic data; omit --snapshot')
    if args.snapshot:
        from cachemonitor.usage_paths import index_location
        if args.snapshot == 'default':
            physical = Path.home()/'AppData/Local/CacheMonitor/usage-index.sqlite.codexon-collection.sqlite'
            args.snapshot = (physical if physical.is_file() else
                index_location().with_name(index_location().name+'.codexon-collection.sqlite'))
        else:
            args.snapshot = Path(args.snapshot)
    if sys.platform != 'win32':
        parser.error('The native preview requires Windows.')
    if args.verify and not args.verification_child:
        return subprocess.run([sys.executable, str(ROOT/'tools/run_ui_checks.py'), '--scale', '1',
            '--timeout', '120', '--', sys.executable, str(Path(__file__).resolve()),
            *sys.argv[1:], '--verification-child'], cwd=ROOT).returncode
    from cachemonitor.fonts import configure_font_rendering, configure_high_dpi, load_bundled_fonts
    configure_font_rendering()
    configure_high_dpi()
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    load_bundled_fonts()
    with tempfile.TemporaryDirectory(prefix='codexon-overlay-preview-') as folder:
        settings = QSettings(str(Path(folder)/'settings.ini'), QSettings.IniFormat)
        settings.setValue('overlay/enabled', True)
        settings.setValue('overlay/height', 720)
        preview = Preview(app, args, settings)
        if args.verify:
            try:
                verify(preview, args.verify.resolve())
                print(f'Verified synthetic overlay: {args.verify.resolve()}', flush=True)
            finally:
                preview.cleanup()
                preview.host.close()
            return 0
        return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
