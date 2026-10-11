"""Compact, reversible exclusion editing for one observed weekly period."""
from bisect import bisect_left

from PySide6.QtCore import QTimer, Signal, Slot

from .i18n import Verbatim, formatted
from .presentation import Button, Choice, Column, Group, Input, Row, Text
from .pricing import usd
from .quota_view import clock
from .ui_details import Details


class _ReasonInput(Input):
    def setText(self, value):
        super().setText(Verbatim(value))


class QuotaExclusions(Group):
    requested = Signal(object)
    restore_requested = Signal(str)
    selection_changed = Signal(object, object, bool)

    def __init__(self):
        super().__init__()
        self.report = {}
        self._report_revision = 0
        self.period = None
        self._weekly = True
        self._context = None
        self._times = []
        self._choice_key = None
        self._preview = None
        self._preview_key = None
        self._preview_request = None
        self._preview_task = None
        self._preview_worker = None
        self._owned_worker = None
        self._preview_prepared = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self._start_preview)
        self._pending = False
        self._restores = []
        self.toggle = Button('구간 제외')
        self.toggle.put(disclosure=True)
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName('집계에서 제외할 구간 선택')
        self.history_toggle = Button('제외 내역 0')
        self.history_toggle.put(disclosure=True)
        self.history_toggle.setCheckable(True)
        self.history_toggle.setAccessibleName('제외 내역 보기')
        self.toggle.toggled.connect(self.set_expanded)

        layout = Column(self)
        layout.setContentsMargins(0, 4, 0, 8)
        layout.setSpacing(6)
        bounds = Row(); bounds.put(flow=False,minColumnWidth=280); bounds.setSpacing(16)
        self.start = Choice(); self.end = Choice()
        for label, choice in (('시작', self.start), ('종료', self.end)):
            row = Row(); row.setSpacing(8)
            text = Text(label); text.put(fontSize=12, color='muted')
            row.addWidget(text)
            choice.put(fontSize=12, height=-1, minWidth=0, wrap=True, stretch=1)
            choice.setAccessibleName('제외 '+label+' 관측')
            row.addWidget(choice)
            bounds.addLayout(row)
            choice.currentIndexChanged.connect(self.update_preview)
        layout.addLayout(bounds)
        reason_row = Row(); reason_row.put(flow=False); reason_row.setSpacing(8)
        field = Row(); field.setSpacing(8)
        label = Text('사유 (선택)'); label.put(fontSize=12, color='muted')
        field.addWidget(label)
        self.reason = _ReasonInput()
        self.reason.put(fontSize=12, height=30, maxLength=200, minWidth=140, maxWidth=560)
        self.reason.setAccessibleName('제외 사유 (선택)')
        self.reason.setPlaceholderText('예: 테스트 실행, 수집 이상')
        field.addWidget(self.reason, 1)
        reason_row.addLayout(field, 1)
        self.apply = Button('제외'); self.apply.put(role='primary'); self.apply.setAccessibleName('선택 구간 제외')
        self.cancel = Button('취소'); self.cancel.put(flat=True, color='muted')
        for button in (self.apply, self.cancel):
            button.put(fontSize=12, height=30)
            reason_row.addWidget(button)
        self.apply.clicked.connect(self.submit)
        self.cancel.clicked.connect(self.cancel_edit)
        layout.addLayout(reason_row)
        self.impact = Text(); self.impact.put(fontSize=12, color='muted', wrap=True)
        layout.addWidget(self.impact)
        self.impact_details = Details('집계 변화', compact=True)
        self.impact_details.toggle.put(fontSize=12, color='muted')
        self.impact_details.body.put(fontSize=12, color='muted')
        layout.addWidget(self.impact_details)

        self.history = Group()
        self.history_layout = Column(self.history)
        self.history_layout.setSpacing(8)
        self.history_toggle.toggled.connect(self.history.setVisible)
        self.message = Text(); self.message.put(fontSize=12, color='muted', wrap=True)
        self.message.hide(); self.history.hide(); self.hide()

    def set_context(self, report, period, weekly=True, cursor=0):
        if report is not self.report:
            self._report_revision += 1
        self.report, self.period = report, period
        self._weekly = weekly
        key = (report.get('home'), period.get('start') if period else None, weekly)
        if key != self._context:
            if self._context is not None and key[0] != self._context[0]:
                self.set_pending(False)
            self._context = key
            self.toggle.setChecked(False)
            self.reason.setText('')
            self._choice_key = None
            self.message.hide()
        self._times = (period or {}).get('series', {}).get('times', [])
        self._cursor = min(cursor, max(0, len(self._times)-1))
        editable = weekly and period is not None and period.get('start') != 'all' and len(self._times) >= 2
        self.toggle.setVisible(weekly)
        self.toggle.setEnabled(editable and not self._pending)
        self.toggle.setToolTip('개별 주기를 선택한 뒤 구간을 제외할 수 있습니다.' if weekly and not editable else '그래프를 드래그하거나 시작과 종료 시각을 선택하세요.')
        if not editable:
            self.toggle.setChecked(False)
        self.history_toggle.setVisible(weekly)
        self.render_history()
        if self.toggle.isChecked():
            self.populate_choices()
            self.update_preview()
        else:
            self._preview_request = None
            self._preview_timer.stop()
            self.selection_changed.emit(None, None, False)

    def populate_choices(self):
        key = (self._context, len(self._times), self._times[-1] if self._times else None)
        if key == self._choice_key or len(self._times) < 2:
            return
        previous = (self.start.currentData(), self.end.currentData())
        items = [dict(text=clock(at, True), value=at, tooltip='') for at in self._times]
        for index, choice in enumerate((self.start, self.end)):
            choice.blockSignals(True)
            # Publish the exact observation list once, avoiding quadratic addItem copies.
            choice._items = items
            choice.put(items=items)
            old = previous[index]
            selected = bisect_left(self._times, old) if old is not None else max(0, self._cursor-1) if index == 0 else max(1, self._cursor)
            choice.setCurrentIndex(min(selected, len(items)-1))
            choice.blockSignals(False)
        self._choice_key = key

    def set_expanded(self, expanded):
        expanded = bool(expanded and self.toggle.isEnabled())
        self.setVisible(expanded)
        if expanded:
            self.populate_choices()
            self.update_preview()
        else:
            self._preview_request = None
            self._preview_timer.stop()
            self.selection_changed.emit(None, None, False)

    def select_range(self, start, end):
        if not self.toggle.isEnabled() or not self._times:
            return
        self.toggle.setChecked(True)
        self.populate_choices()
        for choice, at in ((self.start, min(start, end)), (self.end, max(start, end))):
            index = min(bisect_left(self._times, at), len(self._times)-1)
            choice.blockSignals(True); choice.setCurrentIndex(index); choice.blockSignals(False)
        self.update_preview()

    def update_preview(self, *_):
        start, end = self.start.currentData(), self.end.currentData()
        key = (self._report_revision, self._context, start, end)
        if key == self._preview_key and self._preview is not None:
            self.selection_changed.emit(start, end, True)
            return
        self._preview_request = None
        self._preview = None
        self.apply.setEnabled(False)
        self.impact_details.hide()
        if not self.toggle.isChecked():
            return
        self.selection_changed.emit(start, end, True)
        if start is None or end is None or end <= start:
            self.impact.setText('종료 시각을 시작보다 뒤로 선택하세요.')
            return
        self._preview_request = (key, self.report, start, end)
        self.impact.setText('계산 중...')
        self._preview_timer.start()

    @Slot()
    def _start_preview(self):
        if self._preview_task is not None or self._preview_request is None or not self.toggle.isChecked():
            return
        self._preview_timer.stop()
        key, report, start, end = self._preview_request
        worker = report.get('_worker')
        if worker is None:
            if self._owned_worker is None:
                from .quota_worker import QuotaWorker
                self._owned_worker = QuotaWorker()
                self.destroyed.connect(lambda *_ , worker=self._owned_worker: worker.close())
            worker = self._owned_worker
        if worker is not self._preview_worker:
            if self._preview_worker is not None:
                self._preview_worker.result.disconnect(self._receive_worker_preview)
            self._preview_worker = worker
            worker.result.connect(self._receive_worker_preview)
        revision = report.get('worker_revision')
        if revision is None:
            if self._preview_prepared is not None and self._preview_prepared[0] is report:
                revision = self._preview_prepared[1]
            else:
                identifier = worker.request('prepare', key=key,
                    snapshot={name:value for name,value in report.items() if name not in ('view','_worker')})
                self._preview_task = ('prepare', identifier, key)
                return
        identifier = worker.request('preview', key=key, revision=revision, start=start, end=end)
        self._preview_task = ('preview', identifier, key)

    @Slot(object)
    def _receive_worker_preview(self, result):
        task = self._preview_task
        if (task is None or self.sender() is not self._preview_worker
                or (result['kind'], result['id'], result['key']) != task):
            return
        self._preview_task = None
        key = result['key']
        if result['kind'] == 'prepare' and not result.get('error'):
            if self._preview_request is not None and key == self._preview_request[0]:
                self._preview_prepared = (self.report, result['report']['worker_revision'])
            self._start_preview()
            return
        self._receive_preview(key, result.get('value'), result.get('error'))

    @Slot(object, object, object)
    def _receive_preview(self, key, preview, error):
        self._preview_task = None
        if (self._preview_request is None or key != self._preview_request[0]
                or not self.toggle.isChecked()):
            self._start_preview()
            return
        if error is not None:
            self.impact.setText(error)
            return
        self._preview_key = key
        self._preview = preview
        start, end = self.start.currentData(), self.end.currentData()
        removed = preview['removed']
        self.impact.setText(formatted('새로 제외: {delta}%p / {cost}', delta=f"{removed['delta']:g}", cost=usd(removed['cost'])))
        if (preview['start'], preview['end']) != (start, end):
            self.impact.setText(formatted('{summary} / 적용 구간 {start} ~ {end}', summary=self.impact.text(), start=clock(preview['start'], True), end=clock(preview['end'], True)))
            self.selection_changed.emit(preview['start'], preview['end'], True)
        def value(stats):
            return usd(stats['cost']/stats['delta']*100 if stats['delta']>0 else None)
        sections = []
        if 'period_before' in preview:
            sections.append(('주간 예상 가치', value(preview['period_before'])+' → '+value(preview['period_after'])))
        sections.append(('누적 예상 가치', value(preview['before'])+' → '+value(preview['after'])))
        self.impact_details.set_sections(sections)
        self.impact_details.show()
        self.apply.setEnabled(not self._pending)

    def cancel_edit(self):
        self.toggle.setChecked(False)
        self.reason.setText('')

    def submit(self):
        if self._pending or self._preview is None or not self.apply.isEnabled():
            return
        payload = dict(start=self._preview['start'], end=self._preview['end'], reason=self.reason.text().strip()[:200])
        self.set_pending(True)
        self.requested.emit(payload)

    def restore(self, identifier):
        if self._pending:
            return
        self.set_pending(True)
        self.restore_requested.emit(identifier)

    def set_pending(self, pending):
        self._pending = pending
        for node in (self.start, self.end, self.reason, self.apply, self.cancel, self.toggle, *self._restores):
            node.setEnabled(not pending)
        self.apply.setEnabled(not pending and self._preview is not None)
        self.message.setText('저장 중...' if pending else '')
        self.message.setVisible(pending)

    def receive_result(self, result):
        self.set_pending(False)
        if (result.get('ok') or result.get('saved')) and result.get('operation') == 'exclude':
            self.cancel_edit()
            self.impact_details.toggle.setChecked(False)
        if result.get('ok'):
            self.message.setText('선택 구간을 제외하고 재집계했습니다.' if result.get('operation') == 'exclude' else '해당 제외 내역을 해제하고 재집계했습니다.')
        else:
            self.message.setText(result.get('error') or '제외 내역을 저장하지 못했습니다.')
        self.message.show()

    def render_history(self):
        records = self.report.get('exclusions', [])
        self.history_toggle.setText(formatted('제외 내역 {count}', count=str(len(records))))
        self.history_toggle.setEnabled(bool(records))
        key = tuple((r['id'], r['start'], r['end'], r.get('reason', '')) for r in records)
        if getattr(self, '_history_key', None) == key:
            self.history.setVisible(self._weekly and self.history_toggle.isChecked() and bool(records))
            return
        self._history_key = key
        previous = self.history_layout._nodes
        self.history_layout._nodes = []
        self.history_layout.structureChanged.emit()
        for node in previous:
            node.deleteLater()
        self._restores = []
        for record in records:
            row = Row(); row.setSpacing(12)
            column = Column(); column.setSpacing(2)
            label = Text(Verbatim(clock(record['start'], True)+' ~ '+clock(record['end'], True)))
            label.put(fontSize=12, wrap=True); column.addWidget(label)
            if record.get('reason'):
                reason = Text(Verbatim(record['reason'])); reason.put(fontSize=12, color='muted', wrap=True)
                column.addWidget(reason)
            row.addLayout(column, 1)
            button = Button('복원'); button.put(fontSize=12, height=30)
            button.setAccessibleName(formatted('{start} ~ {end} 제외 복원', start=clock(record['start'], True), end=clock(record['end'], True)))
            button.clicked.connect(lambda identifier=record['id']: self.restore(identifier))
            button.setEnabled(not self._pending)
            self._restores.append(button); row.addWidget(button)
            self.history_layout.addLayout(row)
        self.history.setVisible(self._weekly and self.history_toggle.isChecked() and bool(records))
