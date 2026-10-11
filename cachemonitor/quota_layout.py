"""Usage summary, unified chart legend and modal exclusion management."""
from html import escape
from .presentation import Button, Column, Group, Row, Scroll, Text
from .quick_runtime import Dialog, QuickHost
from .i18n import Verbatim, language, formatted, tr
from .pricing import usd
from .theme import shared_theme

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
    actions.addWidget(panel.manage_button)
    build_exclusion_dialog(panel)
    impact=panel.exclusions.impact_details
    impact.toggle.hide();impact.toggle.setChecked(True)
    impact.changed.connect(lambda: impact.content.setVisible(True))
    for node in (panel.summary_heading,panel.history_controls,panel.result,panel.cycle_value,panel.lifetime_basis,panel.cycle_basis,panel.conversion):
        detach(node)
    summary_card=Group();summary_card.put(background='secondary',border='border',radius=12)
    summary_layout=Column(summary_card);summary_layout.setContentsMargins(24,24,24,24)
    summary_text=Text();summary_text.put(fontSize=22,wrap=True,rich=True)
    summary_layout.addWidget(summary_text)
    panel.layout().insertWidget(0,summary_card)
    panel.summary_card=summary_card
    panel.summary_text=summary_text
    def summarize():
        lifetime=panel.lifetime_statistics
        summary_card.setVisible(bool(lifetime['total'] or panel.report.get('exclusions')))
        palette=shared_theme().palette
        def emphasize(value):
            return f'<span style="font-size:32px;font-weight:700;color:{palette["accent"]}">{escape(value)}</span>'
        usage=emphasize(f"{lifetime['delta']:g}%");cost=emphasize(usd(lifetime['cost']))
        main=(f"You've used {usage} so far, worth {cost}." if language()=='en' else
              f'지금까지 {usage}를 사용했으며, {cost} 상당입니다.')
        basis=(f'(Based on {emphasize(usd(panel.lifetime_value))} for 100% of your weekly allowance.)' if language()=='en' else
               f'(주간 할당량 100% = {emphasize(usd(panel.lifetime_value))} 기준)')
        summary_text.setText(Verbatim(main+'<br>'+basis))
    panel.result.textValueChanged.connect(summarize)
    panel.update_summary=summarize
    shared_theme().changed.connect(summarize)
    summary_text.destroyed.connect(lambda *_: shared_theme().changed.disconnect(summarize))
    summarize()
    labels=dict(remaining='남은 사용량',cycle_cost='누적 측정 환산액',
                cycle_value='주간 예상 가치',reference='누적 예상 가치')
    def label_for(key):
        return '누적 사용량' if key=='remaining' and panel.history.series.get('cumulative') else labels[key]
    def legend_values(values):
        for key,button in panel.legend_toggles.items():
            button.setText(formatted('{label}  {value}',label=tr(label_for(key)),value=values.get(key,'—')))
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
        editor.impact_details.toggle.setChecked(True)
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
