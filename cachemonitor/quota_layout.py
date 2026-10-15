"""Usage summary, unified chart legend and modal exclusion management."""
from time import time
from .presentation import Button, Column, Group, Row, Scroll, Text
from .quick_runtime import Dialog, QuickHost
from .i18n import Verbatim, formatted, tr
from .pricing import usd
from .theme import shared_theme
from .token_colors import mix

def detach(node):
    parent = node.parent()
    parent._nodes.remove(node)
    parent.structureChanged.emit()
    return parent


def configure_quota_layout(panel):
    actions = panel.cycle_choice.parent()
    actions.put(flow=False)
    panel.cycle_choice.put(wrap=True)
    for node in (panel.exclusions.toggle,panel.exclusions.history_toggle,panel.exclusions,
                 panel.exclusions.history,panel.exclusions.message):
        detach(node)
    panel.manage_button=Button('제외 관리');panel.manage_button.put(fontSize=12,height=30)
    panel.manage_button.hide()
    panel.clear_cycle_button=Button('선택 주기 초기화');panel.clear_cycle_button.put(fontSize=12,height=30)
    panel.clear_cycle_button.setEnabled(False)
    actions.addWidget(panel.clear_cycle_button)
    actions.addWidget(panel.manage_button)
    panel.summary_context=Row();panel.summary_context.put(collapseBelow=600,columns=1)
    panel.summary_note=Text('주기마다 한 점으로 표시합니다. 점을 누르면 해당 주기의 상세 차트를 엽니다.')
    panel.summary_note.put(wrap=True,fontSize=13,color='muted')
    panel.summary_back=Button('전체 보기');panel.summary_back.put(fontSize=13,height=32)
    panel.summary_back.clicked.connect(panel.return_to_summary)
    panel.summary_context.addWidget(panel.summary_note,1);panel.summary_context.addWidget(panel.summary_back)
    legend_parent=panel.history_legend.parent()
    legend_parent.insertWidget(legend_parent._nodes.index(panel.history_legend),panel.summary_context)
    panel.cycle_clear_message=Text();panel.cycle_clear_message.put(fontSize=12,wrap=True)
    panel.cycle_clear_message.hide()
    chart_layout=actions.parent()
    chart_layout.insertWidget(chart_layout._nodes.index(actions)+1,panel.cycle_clear_message)
    def clear_cycle():
        from .quick_runtime import Confirmation
        from .quota_view import clock
        selected=next((p for p in panel._view['periods'] if p['start']==panel.cycle_choice.currentData()),None)
        if selected is None or not panel.report.get('account') or getattr(panel,'_cycle_clear_pending',False):return
        delete=not selected['current']
        request=dict(start=selected['start'],account=panel.report['account'],delete=delete)
        home=panel.report.get('home')
        title='선택 주기 삭제' if delete else '선택 주기 초기화'
        message=('선택한 주기와 측정 기록을 삭제합니다. 이 작업은 되돌릴 수 없습니다.' if delete else
                 '선택한 주기의 측정 기록을 지우고 지금부터 다시 측정합니다. 이전 주기 기록은 유지됩니다.')
        dialog=Confirmation(title,formatted('{start} → {end}\n\n{message}',
            start=clock(selected['start'],True),end=clock(selected['end'],True),message=tr(message)),panel)
        panel.clear_cycle_dialog=dialog
        dialog.confirm.setText('삭제' if delete else '초기화')
        def finish(result):
            if not result or home!=panel.report.get('home') or request['account']!=panel.report.get('account'):return
            panel._cycle_clear_pending=True;panel.clear_cycle_button.setEnabled(False)
            panel.cycle_clear_message.hide()
            panel.cycle_clear_requested.emit(request)
        dialog.finished.connect(finish);dialog.open()
    panel.clear_cycle_button.clicked.connect(clear_cycle)
    panel.render_history()
    build_exclusion_dialog(panel)
    for node in (panel.summary_heading,panel.history_controls,panel.result,panel.cycle_value,panel.lifetime_basis,panel.cycle_basis,panel.conversion):
        detach(node)
    summary=Row();summary.put(collapseBelow=980,columns=2);summary.setSpacing(12)
    panel.layout().insertWidget(0,summary)
    panel.summary=summary
    panel.summary_cards={};panel.summary_values={}
    metrics=(('elapsed','실측 시간'),('usage','누적 사용량'),
             ('cost','누적 가치'),('weekly','주간 할당량 가치'))
    for index,(key,title) in enumerate(metrics):
        if index%2==0:
            pair=Row();pair.put(collapseBelow=1,columns=2);pair.setSpacing(12)
            summary.addWidget(pair,1)
        card=Group();card.put(background='secondary',radius=12)
        content=Column(card);content.setContentsMargins(20,20,20,20);content.setSpacing(12)
        label=Text(title);label.put(fontSize=16,color='muted',wrap=True)
        number=Text();number.put(fontSize=32,bold=True,color='ink',noElide=True,wrap=True,fitText=True)
        content.addWidget(label);content.addWidget(number);pair.addWidget(card,1)
        panel.summary_cards[key]=card;panel.summary_values[key]=number
    panel.summary_values['elapsed'].setToolTip('측정 시작부터 현재까지의 경과 시간입니다. 측정 중지와 수집 공백을 포함합니다.')
    panel.summary_values['usage'].setToolTip('관측한 주간 할당량 소모율의 누적 합계입니다. 초기화 전후의 사용량을 합산합니다.')
    panel.summary_values['weekly'].setToolTip('주간 할당량 100%에 해당하는 추정 가치입니다.')
    def summarize():
        lifetime=panel.lifetime_statistics
        summary.setVisible(bool(lifetime['total'] or panel.report.get('exclusions')))
        palette=shared_theme().palette
        border=mix(palette['secondary'],palette['accent'],.35)
        for card in panel.summary_cards.values():card.put(border='transparent' if panel.monitoring_paused else border)
        started=(panel.report.get('tracking') or {}).get('started')
        if started is None:
            started=(panel.report.get('view') or {}).get('tracking_started')
        now=panel.monitoring_paused_at if panel.monitoring_paused and panel.monitoring_paused_at else time()
        elapsed=(f'{max(0,now-started)/3600:,.1f}'.removesuffix('.0')+' hr'
                 if started is not None else '—')
        values=dict(elapsed=elapsed,usage=f"{lifetime['delta']:g}%",
                    cost=usd(lifetime['cost']),weekly=usd(panel.lifetime_value))
        if panel.report.get('history_pending'):
            for key in ('usage','cost','weekly'):
                values[key]=tr('확인 불가' if panel.report.get('history_failed') else '계산 중')
        for key,value in values.items():panel.summary_values[key].setText(Verbatim(value))
    panel.result.textValueChanged.connect(summarize)
    panel.update_summary=summarize
    shared_theme().changed.connect(summarize)
    summary.destroyed.connect(lambda *_: shared_theme().changed.disconnect(summarize))
    summarize()
    labels=dict(remaining='남은 사용량',cycle_cost='누적 측정 환산액',
                cycle_value='주간 예상 가치',reference='누적 예상 가치')
    def label_for(key):
        if panel.history.series.get('cycle_summary'):
            return dict(remaining='주기 소모량',cycle_cost='주기 환산액').get(key,labels[key])
        return labels[key]
    def legend_values(values):
        for key,button in panel.legend_toggles.items():
            pattern=('{label}  {value}' if panel.history.series.get('cycle_summary') else
                     '{label} (평균: {value})' if key=='cycle_value' else '{label} (최근: {value})')
            button.setText(formatted(pattern,label=tr(label_for(key)),value=values.get(key,'—')))
    panel.history.legendChanged.connect(legend_values)
    legend_values(panel.history.legend_values())
    order=('cycle_value','remaining','cycle_cost')
    colors={'cycle_value':'accent','remaining':'written','cycle_cost':'output','reference':'accent'}
    for key,button in panel.legend_toggles.items():button.put(legendColor=colors[key])
    panel.legend_toggles['cycle_value'].setToolTip('선택 주기의 사용 기록으로 계산한 주간 예상 가치')
    extras=[node for node in panel.history_legend._nodes if node not in panel.legend_toggles.values()]
    panel.history_legend._nodes=[panel.legend_toggles[key] for key in order]+extras
    panel.history_legend.structureChanged.emit()


def build_exclusion_dialog(panel):
    editor=panel.exclusions
    dialog=Dialog(panel);dialog.setWindowTitle('제외 관리')
    panel.dialog=dialog
    outer=Column(dialog);outer.setContentsMargins(20,20,20,20);outer.setSpacing(14)
    title=Text('제외 관리');title.put(fontSize=22,bold=True);outer.addWidget(title)
    scroll=Scroll();outer.addWidget(scroll,1)
    management=Row();management.put(minColumnWidth=420);management.setSpacing(16)
    panel.management=management;scroll.setWidget(management)
    panel.dialog_scroll=scroll
    for name,content in (('구간 제외',editor),('제외 내역',editor.history)):
        section=Group();section.put(background='secondary',border='border',radius=8)
        column=Column(section);column.setContentsMargins(14,14,14,14);column.setSpacing(10)
        heading=Text(name);heading.put(fontSize=16,bold=True)
        if content is editor:
            heading_row=Row();heading_row.addWidget(heading);heading_row.addStretch()
            select=Button('그래프에서 선택');select.put(fontSize=12,height=30)
            heading_row.addWidget(select);column.addLayout(heading_row)
            panel.select_graph=select
            def select_graph():
                panel._return_to_chart=True;dialog.reject()
            select.clicked.connect(select_graph)
            panel.exclude_heading=heading
            panel.exclude_hint=Text('개별 주기를 선택한 뒤 구간을 제외할 수 있습니다.')
            panel.exclude_hint.put(fontSize=12,color='muted',wrap=True)
            column.addWidget(panel.exclude_hint)
        else:
            column.addWidget(heading)
            panel.history_heading=heading
            panel.exclusion_history_empty=Text('제외 내역이 없습니다.');panel.exclusion_history_empty.put(fontSize=13,color='muted')
            column.addWidget(panel.exclusion_history_empty)
        column.addWidget(content);management.addWidget(section,1)
    outer.addWidget(editor.message)
    footer=Row();footer.addStretch();close=Button('닫기');footer.addWidget(close);outer.addLayout(footer)
    panel.dialog_close=close;close.clicked.connect(dialog.reject)
    editor.cancel.clicked.connect(dialog.reject)
    def sync():
        editable=editor.toggle.isEnabled() and panel.cycle_choice.currentData()!='all'
        editor.setEnabled(editable);editor.show()
        panel.select_graph.setEnabled(editable)
        panel.exclude_hint.setVisible(not editable)
        records=panel.report.get('exclusions',[])
        panel.history_heading.setText(formatted('제외 내역 {count}',count=len(records)))
        panel.exclusion_history_empty.setVisible(not records)
        editor.history.setVisible(bool(records))
        panel.history.put(manageExclusions=editable)
    panel.sync_dialog=sync
    def open_dialog(*_):
        if dialog.host:
            dialog.host.raise_();dialog.host.activateWindow();return
        editable=editor.toggle.isEnabled() and panel.cycle_choice.currentData()!='all'
        editor.toggle.setChecked(editable)
        editor.history_toggle.setChecked(True)
        sync()
        host=host_for(panel)
        if host is None:return
        width=min(1080,max(480,host.width()-40))
        dialog.resize(width,min(host.height()-100,580 if width<880 else 420))
        dialog.open()
    panel.open_dialog=open_dialog
    panel.manage_button.clicked.connect(open_dialog)
    panel.history.range_selected.connect(open_dialog)
    def closed(*_):
        dialog.setParent(panel);dialog.host=None;editor.toggle.setChecked(False)
        panel.history.put(manageExclusions=editor.toggle.isEnabled() and panel.cycle_choice.currentData()!='all')
        if getattr(panel,'_return_to_chart',False):
            panel._return_to_chart=False
            reveal_chart(panel)
            host=host_for(panel)
            if host is not None:host.activateWindow()
    dialog.finished.connect(closed)
    panel.cycle_choice.currentIndexChanged.connect(lambda *_: panel.history.put(
        manageExclusions=editor.toggle.isEnabled() and panel.cycle_choice.currentData()!='all'))
    panel.history.put(manageExclusions=editor.toggle.isEnabled() and panel.cycle_choice.currentData()!='all')


def host_for(panel):
    node=panel
    while node is not None:
        if isinstance(node,QuickHost):return node
        node=node.parent()
    return None


def reveal_chart(panel):
    node=panel.parent()
    while node is not None:
        if isinstance(node,Scroll):node.ensureWidgetVisible(panel.history);return
        node=node.parent()


def refresh_monitoring_style(panel):
    from .quota_chart import monitoring_palette
    paused=panel.monitoring_paused
    opacity=.42 if paused else 1
    note=getattr(panel,'monitoring_note',None)
    for node in panel.layout()._nodes:
        if node is not note:node.put(opacity=opacity)
    periods={period['start']:period for period in getattr(panel,'_view',{}).get('periods',[])}
    items=[]
    for item in panel.cycle_choice._items:
        period=periods.get(item['value'])
        text=period['label'] if period else item['text']
        items.append(dict(item,text=text.replace(' (진행 중)','') if paused else text,
            ink='ink' if paused or not period or not period.get('current') else 'accent'))
    panel.cycle_choice._items=items;panel.cycle_choice.put(items=list(items))
    palette=monitoring_palette(paused)
    for key,color in (('cycle_value','accent'),('remaining','written'),('cycle_cost','output')):
        panel.legend_toggles[key].put(legendColor=palette[color] if paused else color)


def configure_monitoring_controls(window):
    header=window.heading.parent()
    badge=window.quota_pause_badge=Group();badge.put(background='secondary',radius=5)
    row=Row(badge);row.setContentsMargins(9,5,9,5);row.setSpacing(7)
    icon=Group();icon.setFixedWidth(10);icon.setFixedHeight(12)
    bars=Row(icon);bars.setSpacing(3)
    for _ in range(2):
        bar=Group();bar.setFixedWidth(3);bar.setFixedHeight(12);bar.put(background='muted',radius=1);bars.addWidget(bar)
    row.addWidget(icon)
    window.quota_pause_status=Text('일시정지');window.quota_pause_status.put(fontSize=12,color='muted')
    row.addWidget(window.quota_pause_status)
    header.insertWidget(header._nodes.index(window.heading)+1,badge)
    button=window.quota_pause_button=Button('일시정지')
    button.clicked.connect(lambda:window.set_quota_paused(not window.quota_paused))
    header.insertWidget(header._nodes.index(window.price_button),button)
    note=window.quota_panel.monitoring_note=Text();note.put(fontSize=13,color='muted',wrap=True)
    window.quota_panel.layout().insertWidget(0,note)
    shared_theme().changed.connect(window.refresh_quota_monitoring)
    refresh_monitoring_controls(window)


def refresh_monitoring_controls(window):
    from datetime import datetime
    from .i18n import language
    paused=window.quota_paused
    pending=window.quota_pause_pending
    page=window.current_page==3
    button=window.quota_pause_button
    button.setVisible(page);button.setEnabled(not pending)
    pause_action=Verbatim('Pause') if language()=='en' else '일시정지'
    button.setText('재개 중...' if pending and not paused else '일시정지 중...' if pending else '모니터링 재개' if paused else pause_action)
    button.setAccessibleName('이 기기의 구독 모니터링 재개' if paused else '이 기기의 구독 모니터링 일시정지')
    window.quota_pause_badge.setVisible(page and (paused or pending))
    window.quota_pause_status.setText('연결 중' if pending and not paused else '일시정지')
    note=window.quota_panel.monitoring_note;note.setVisible(paused or pending)
    if pending and not paused:note.setText('현재 잔여량을 확인하고 있습니다.')
    elif paused:
        stamp=datetime.fromtimestamp(window.quota_paused_at).strftime('%H:%M') if window.quota_paused_at else '—'
        note.setText(formatted('{time}에 멈춤. 저장된 기록을 보고 있습니다.',time=stamp))
    window.price_button.put(opacity=.42 if page and (paused or pending) else 1)
    # Retain the paused appearance until the first resumed lookup completes.
    display_paused=paused or pending
    window.quota_panel.set_monitoring_paused(display_paused,window.quota_paused_at)
