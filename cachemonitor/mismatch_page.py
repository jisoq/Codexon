from datetime import datetime

from PySide6.QtCore import Qt

from .analytics import call_identity
from .dashboard_presentation import RecordFields
from .history_navigation import history_duration
from .i18n import Verbatim, tr
from .mismatch_history import COLUMNS
from .presentation import Button, Column, Group, Row, Scroll, Text
from .pricing import request_tier
from .table_model import LazyTable
from .ui_details import Details, observed_transport, recorded


TEXT_RUNS = Qt.UserRole + 7
SOURCES = {'wire': '실제 요청', 'response': '실제 응답', 'turn_context': '로컬 턴 기록',
           'context': '로컬 턴 기록', 'settings': '당시 적용 설정', 'applied_settings': '당시 적용 설정',
           'response_id': '응답에 연결된 통신 기록', 'conflict': '관측 충돌', 'unknown': '미확인'}
CORRECTIONS = {'model_conflict': '모델 관측 기록 충돌', 'model_agrees': '모델 일치로 정정',
               'confirmation_withdrawn': '불일치 확인 철회', 'conflicting_occurrences': '같은 호출의 현재 관측 기록 충돌'}


def source_text(value, response=False):
    if not value:
        return None
    return tr('실제 응답' if response and value == 'wire' else SOURCES.get(value, '미확인'))


def correction_text(row):
    value = row.get('correction_reason')
    return tr(CORRECTIONS.get(value, value)) if value else None


def _timestamp(value):
    return datetime.fromtimestamp(value).strftime('%Y/%m/%d %H:%M:%S') if value is not None else tr('미확인')


def _number(value):
    return f'{value:,}' if value is not None else tr('미확인')


def model_values(row, response=False):
    if response:
        model = row.get('response_model')
        effort = row.get('response_effort') if row.get('response_effort_confirmed') and not row.get('response_effort_conflict') else None
        tier = row.get('response_service_tier') if row.get('response_tier_confirmed') else None
        mode = request_tier({'service_tier': tier})
    else:
        model = row.get('requested_model')
        effort = None if row.get('requested_effort_conflict') else row.get('requested_effort') or row.get('effort')
        mode = request_tier(row)
    return tuple(str(value) if recorded(value) else tr('미확인') for value in (model, effort, mode))


def model_text(row, response=False):
    return Verbatim(' / '.join(model_values(row, response)))


def response_runs(row):
    model, effort, mode = model_values(row, True)
    return [dict(text=model, color='agent:'+(row.get('sid') or '')), dict(text=' / '+effort+' / '+mode)]


def record_identity(row):
    return tuple(row['identity']) if row.get('identity') else call_identity(row['home'], row['sid'], row['key'])


def status_text(row):
    return tr('확인 정정됨') if row.get('correction_at') is not None else tr('모델명 불일치')


def cell_value(row, column, role):
    if row.get('_placeholder'):
        return [] if role == TEXT_RUNS else ''
    if role == TEXT_RUNS:
        if column != 2:
            return []
        return response_runs(row)
    if column in (1, 2):
        response = column == 2
        text = model_text(row, response)
        if role == Qt.ToolTipRole:
            source = (row.get('response_effort_source') if row.get('response_effort_confirmed') and not row.get('response_effort_conflict') else None) if response else row.get('requested_effort_source')
            if not response and not source and recorded(row.get('effort')):
                source = row.get('effort_source') or 'turn_context'
            provenance = source_text(source, response)
            notes = [text]
            if provenance:
                notes.append(tr('추론 설정 근거')+'  '+provenance)
            if row.get('response_effort_conflict' if response else 'requested_effort_conflict'):
                notes.append(tr('추론 설정 기록 충돌'))
            if not response:
                mode_source = row.get('request_mode_source') or row.get('service_tier_source')
                if mode_source:
                    notes.append(tr('모드 근거')+'  '+source_text(mode_source))
            return Verbatim('\n'.join(notes))
        return text
    value = ({0: _timestamp(row.get('ts')), 3: _number(row.get('input')),
              4: _number(row.get('output')), 5: _number(row.get('reasoning')),
              6: f"{row['rate']:.1f}%" if row.get('rate') is not None else tr('미확인'),
              7: history_duration(row.get('duration')) if row.get('duration') is not None else tr('측정 불가'),
              8: f"{row['output_speed']:,.1f} tok/s" if row.get('output_speed') is not None else tr('측정 불가'),
              9: status_text(row)}).get(column, '')
    if role == Qt.ToolTipRole and column == 9:
        parts = [value]
        if row.get('response_status'):
            parts.append(tr('응답 상태')+'  '+str(row['response_status']))
        if row.get('correction_reason'):
            parts.append(tr('정정 사유')+'  '+correction_text(row))
        return Verbatim('\n'.join(parts))
    return Verbatim(value)


class MismatchPage(Group):
    def __init__(self, *, reader=None):
        super().__init__()
        self.worker = None
        self.reader = reader
        self.active = False
        self.page_request = self.record_request = 0
        self.revision = None
        self.health = {}
        self.last_attempt = None
        self.selected_identity = None
        self.saved_record = None
        self.query_errors = {'page': '', 'record': ''}
        layout = Column(self)
        layout.setSpacing(10)
        toolbar = Row()
        title = Text('모델명 불일치 기록')
        title.setObjectName('section')
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.close_button = Button('상세 닫기')
        self.close_button.clicked.connect(self.close_detail)
        self.close_button.hide()
        toolbar.addWidget(self.close_button)
        self.retry_button = Button('다시 시도')
        self.retry_button.clicked.connect(self.retry)
        toolbar.addWidget(self.retry_button)
        layout.addLayout(toolbar)
        self.error = Text()
        self.error.setWordWrap(True)
        self.error.put(color='warning', selectable=True)
        self.error.hide()
        layout.addWidget(self.error)
        self.table = LazyTable(list(COLUMNS))
        self.table.put(rowHeight=40, rowDetails=True, readableColumns=True,
                       leftColumns=[0, 1, 2, 9], styledColumns=[2],
                       widths=[180, 260, 260, 88, 88, 88, 118, 118, 148, 148],
                       emptyText='모델명 불일치 기록 없음', footerText=self.timezone_text())
        self.table.live_update = True
        self.table.rangeRequested.connect(self.request_page)
        self.table.cellClicked.connect(self.open_record)
        self.table.cellActivated.connect(self.open_record)
        layout.addWidget(self.table, 1)
        self.fields = RecordFields()
        detail_scroll = Scroll()
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        detail_scroll.setWidget(self.fields)
        detail_scroll.setMinimumHeight(140)
        detail_scroll.setMaximumHeight(220)
        self.detail = Details('기록 상세', detail_scroll, compact=True)
        self.detail.toggle.setChecked(True)
        self.detail.hide()
        layout.addWidget(self.detail)

    @staticmethod
    def timezone_text():
        now = datetime.now().astimezone()
        offset = now.strftime('%z')
        return Verbatim(f"{now.tzname()} (UTC{offset[:3]}:{offset[3:]})")

    def set_worker(self, worker):
        self.worker = worker
        worker.mismatch_page.connect(self.page_ready)
        worker.mismatch_record.connect(self.record_ready)
        self.last_attempt = None

    def set_active(self, active):
        if self.active == active:
            return
        self.active = active
        if not active:
            self.page_request += 1
            self.record_request += 1
        else:
            self.last_attempt = None

    def receive_health(self, health):
        self.health = dict(health or {})
        self.update_error()

    def update_error(self):
        messages = []
        status = self.health.get('status')
        if status == 'capture_gap' or self.health.get('gap'):
            messages.append(tr('일부 불일치 기록을 저장하지 못했습니다. 현재 저장된 기록만 표시합니다.'))
        elif status == 'retry_pending':
            messages.append(tr('불일치 기록 저장을 다시 시도하고 있습니다. 저장 전 기록은 보존되지 않을 수 있습니다.'))
        if self.health.get('error'):
            messages.append(str(self.health['error']))
        messages.extend(value for value in self.query_errors.values() if value)
        self.error.setText(Verbatim('\n'.join(messages)))
        self.error.setVisible(bool(messages))

    def refresh(self, *, automatic=False):
        if not self.active:
            return
        self.table.put(footerText=self.timezone_text())
        first = max(0, self.table.first_visible - 32)
        size = max(128, min(512, (self.table.last_visible - self.table.first_visible + 1) * 3))
        signature = (self.health.get('revision'), first, size)
        if automatic and signature == self.last_attempt:
            return
        self.request_page()

    def request_page(self):
        if not self.active or self.worker is None and self.reader is None:
            return
        start = max(0, self.table.first_visible - 32)
        size = max(128, min(512, (self.table.last_visible - self.table.first_visible + 1) * 3))
        if not self.table.rowCount():
            self.table.put(emptyText='기록을 읽는 중')
        self.last_attempt = (self.health.get('revision'), start, size)
        self.page_request += 1
        request_id = self.page_request
        if self.worker is not None:
            self.worker.request_mismatch_page(request_id, start=start, size=size)
        else:
            try:
                result = self.reader.window(start=start, size=size)
            except Exception as error:
                result = dict(error=str(error))
            self.page_ready(dict(result, id=request_id))

    def page_ready(self, value):
        if not self.active or value.get('id') != self.page_request:
            return
        if value.get('error'):
            self.table.put(emptyText='불일치 기록을 읽지 못했습니다.')
            self.query_errors['page'] = tr('불일치 기록을 읽지 못했습니다.')+' '+str(value['error'])
            self.update_error()
            return
        self.table.put(emptyText='모델명 불일치 기록 없음')
        if value.get('capture_gap'):
            self.health['gap']=True
        self.query_errors['page'] = ''
        self.update_error()
        changed = self.revision != value.get('revision')
        self.revision = value.get('revision')
        self.table.set_window(value['window'], cell_value, 'mismatch')
        if self.selected_identity and (changed or self.saved_record is None):
            self.request_record()

    def open_record(self, index, _column=0):
        if not 0 <= index < self.table.rowCount():
            return
        row = self.table.model().rows[index]
        if row.get('_placeholder'):
            return
        identity = record_identity(row)
        if identity != self.selected_identity:
            self.saved_record = None
            self.detail.hide()
        self.selected_identity = identity
        self.close_button.show()
        self.request_record()

    def request_record(self):
        if not self.active or not self.selected_identity:
            return
        self.record_request += 1
        request_id = self.record_request
        if self.worker is not None:
            self.worker.request_mismatch_record(request_id, self.selected_identity)
        elif self.reader is not None:
            try:
                result = self.reader.record(self.selected_identity)
            except Exception as error:
                result = dict(error=str(error))
            self.record_ready(dict(result, id=request_id))

    def record_ready(self, value):
        if not self.active or value.get('id') != self.record_request:
            return
        if value.get('error'):
            self.query_errors['record'] = tr('저장된 기록 상세를 읽지 못했습니다.')+' '+str(value['error'])
            self.update_error()
            return
        row = value.get('row')
        if row is None:
            self.query_errors['record'] = tr('저장된 기록을 찾을 수 없습니다.')
            self.update_error()
            return
        if record_identity(row) != self.selected_identity:
            return
        self.query_errors['record'] = ''
        self.update_error()
        self.saved_record = row
        entries = [('기록 시각', _timestamp(row.get('ts'))),
                   ('호출 모델', model_text(row)), ('응답모델', model_text(row, True)),
                   ('상태', status_text(row)), ('응답 상태', row.get('response_status')),
                   ('통신 방식', observed_transport(row) or tr('미확인')),
                   ('통신 근거', source_text(row.get('transport_source'))),
                   ('최초 저장 시각', _timestamp(row.get('first_captured_at'))),
                   ('마지막 변경 시각', _timestamp(row.get('changed_at'))),
                   ('캐시 읽기', _number(row.get('cached'))), ('캐시 쓰기', _number(row.get('written'))),
                   ('요청 추론 설정 근거', source_text(row.get('requested_effort_source') or row.get('effort_source'))),
                   ('응답 추론 설정 근거', source_text(row.get('response_effort_source'), True) if row.get('response_effort_confirmed') and not row.get('response_effort_conflict') else None),
                   ('요청 모드 근거', source_text(row.get('request_mode_source') or row.get('service_tier_source')))]
        if row.get('correction_at') is not None:
            entries.extend([('정정 시각', _timestamp(row['correction_at'])), ('정정 사유', correction_text(row))])
        text = Verbatim('\n'.join(tr(title)+'  '+str(value) for title, value in entries if value is not None and value != ''))
        self.fields.set_content(text, [(tr(title), value) for title, value in entries], literal=True,
                                text_runs={tr('응답모델'): response_runs(row)})
        self.detail.show()
        self.close_button.show()

    def close_detail(self):
        self.record_request += 1
        self.selected_identity = None
        self.saved_record = None
        self.query_errors['record'] = ''
        self.update_error()
        self.detail.hide()
        self.close_button.hide()

    def retry(self):
        self.request_page()
        if self.selected_identity:
            self.request_record()
