"""Exact model comparison from response-linked observations, through Qt rendering."""
import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFontMetrics, QHelpEvent
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolTip

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session
from cachemonitor.model_evidence import EvidenceReader, EvidenceStore
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.overlay_chrome import OverlayChrome, OverlayDetail
from cachemonitor.overlay_view import SessionOverlay, font


class ObservedOverlay:
    def __init__(self, tmp_path):
        self.app = QApplication.instance() or QApplication([])
        self.session = Session('model-summary', 'fixture', title='모델 비교 검증')
        self.store = EvidenceStore(tmp_path / 'wire.sqlite')
        self.reader = EvidenceReader(tmp_path / 'wire.sqlite')
        self.engine = AnalysisEngine()
        self.summaries = OverlaySummaries()
        self.widget = SessionOverlay()
        self.header = OverlayChrome('header')
        self.header.resize(268, 36)
        self.detail = None
        self.widget.content_model.put(reducedMotion=True)

    def call(self, key, configured='gpt-6-astra'):
        ts = 1789884000 + len(self.session.requests)
        self.session.add_usage(ts, key,
            dict(input_tokens=1000, cached_input_tokens=800, cache_write_input_tokens=0,
                 output_tokens=50, reasoning_output_tokens=10),
            configured, 'turn-' + key, 'high', service_tier='Standard')
        return ts

    def observe(self, key, requested='gpt-6-astra', response='gpt-6-astra', attempt=None, **changes):
        values = dict(status='completed', conflict=False, observation_missing=False)
        values.update(changes)
        self.store.write('fixture', attempt or 'attempt-' + key, 1789884000, 'WebSocket', key,
                         requested_model=requested, response_model=response, **values)

    def refresh(self):
        self.reader.poll()
        source = self.session.view(1789884100)
        source['history'] = self.reader.enrich('fixture', source['history'])
        self.engine.ingest([source])
        data = self.summaries.collect(self.engine)[0]
        self.widget.set_content(data)
        self.header.set_title(data['title'], self.widget.content_model.context()[0])
        self.widget.show()
        self.header.show()
        QTest.qWait(50)
        assert not self.widget.qml_errors
        return data

    def close(self):
        if self.detail is not None:
            self.detail.close()
        QToolTip.hideText()
        self.header.close()
        self.widget.close()
        self.reader.close()
        self.store.close()
        self.app.processEvents()

    def reveal_comparison(self, expected):
        model = self.widget.content_model
        self.widget.set_layout(detail=True)
        assert self.widget.width() == model.panel_width()
        self.detail = OverlayDetail(model)
        self.detail.resize(240, 640)
        self.detail.show()
        QTest.qWait(50)
        item = next(item for item in model.detail_items()
                    if (''.join(item[0]) if isinstance(item[0], list) else item[0]) == expected)
        viewport = self.detail.quick.rootObject().findChild(QQuickItem, 'detailScroll')
        top = item[2] * model.appearance.scale
        viewport.setProperty('contentY', min(top, viewport.property('contentHeight') - viewport.height()))
        QTest.qWait(30)
        assert model.detailBody.scroll_offset == viewport.property('contentY')
        assert 0 <= top - viewport.property('contentY')
        assert top + item[4] * model.appearance.scale - viewport.property('contentY') <= viewport.height()
        rendered = self.detail.quick.rootObject().findChild(QQuickItem, 'detailBody')
        assert rendered.property('source') is model.detailBody
        assert not self.detail.qml_errors
        return self.detail


@pytest.fixture
def observed_overlay(tmp_path):
    value = ObservedOverlay(tmp_path)
    try:
        yield value
    finally:
        value.close()


def detail_values(model):
    return [''.join(item[0]) if isinstance(item[0], list) else item[0]
            for item in model.detail_items()]


def comparison_lines(value):
    return [line for line in value.splitlines()
            if '모델 일치 (' in line or '모델 불일치 (' in line]


@pytest.mark.parametrize('response,expected', [
    ('gpt-6-astra', '모델 일치 (gpt-6-astra)'),
    ('gpt-5.6-sol', '모델 불일치 (요청:gpt-6-astra, 응답:gpt-5.6-sol)'),
])
def test_complete_observation_has_exact_summary_on_monitor_and_detail(observed_overlay, tmp_path, response, expected):
    fixture = observed_overlay
    fixture.call('call-0')
    fixture.observe('call-0', response=response)
    fixture.refresh()
    widget = fixture.widget
    model = widget.content_model
    assert model.context()[0] == expected
    assert expected in detail_values(model)
    assert expected in widget.accessibleName()
    assert expected in model.detailBody.state['accessible']
    assert widget.toolTip() == expected
    assert fixture.header.toolTip() == expected
    position = QPoint(20, 20)
    QApplication.sendEvent(fixture.header, QHelpEvent(QEvent.ToolTip, position, fixture.header.mapToGlobal(position)))
    assert QToolTip.text() == expected
    QToolTip.hideText()
    detail = fixture.reveal_comparison(expected)
    assert widget.grab().save(str(tmp_path / 'model-summary.png'))
    assert detail.grab().save(str(tmp_path / 'model-summary-detail.png'))
    assert not widget.qml_errors


@pytest.mark.parametrize('evidence', [
    None,
    dict(requested='gpt-6-astra', response=''),
    dict(requested='', response='gpt-6-astra'),
    dict(status='pending'),
    dict(conflict=True),
    dict(observation_missing=True),
])
def test_incomplete_or_conflicting_evidence_never_claims_model_comparison(observed_overlay, evidence):
    fixture = observed_overlay
    fixture.call('call-0')
    if evidence is not None:
        fixture.observe('call-0', **evidence)
    fixture.refresh()
    widget = fixture.widget
    model = widget.content_model
    visible = '\n'.join([*model.context(), widget.accessibleName(),
                         widget.toolTip(), *detail_values(model)])
    assert comparison_lines(visible) == []
    assert not widget.qml_errors


def test_new_call_clears_old_comparison_then_uses_its_own_completed_evidence(observed_overlay):
    fixture = observed_overlay
    fixture.call('old-call')
    fixture.observe('old-call', requested='gpt-6-astra', response='gpt-5.6-sol')
    fixture.refresh()
    model = fixture.widget.content_model
    old = '모델 불일치 (요청:gpt-6-astra, 응답:gpt-5.6-sol)'
    assert model.context()[0] == old
    fixture.call('new-call', configured='gpt-5.6-luna')
    fixture.refresh()
    assert model.selected()['key'] == 'new-call'
    visible = '\n'.join([*model.context(), fixture.widget.accessibleName(),
                         fixture.widget.toolTip(), *detail_values(model)])
    assert comparison_lines(visible) == []
    assert 'gpt-5.6-sol' not in visible
    fixture.observe('new-call', requested='gpt-5.6-luna', response='gpt-5.6-luna')
    fixture.refresh()
    latest = '모델 일치 (gpt-5.6-luna)'
    assert model.context()[0] == latest
    assert latest in detail_values(model) and fixture.widget.toolTip() == latest
    # Inspecting an older call changes only the call-detail comparison.
    model.detailGraph.key(Qt.Key_Home)
    assert model.selected()['key'] == 'old-call'
    assert old in detail_values(model)
    assert model.context()[0] == latest and fixture.widget.toolTip() == latest


def test_long_model_names_are_complete_and_wrapped_in_detail(observed_overlay, tmp_path):
    fixture = observed_overlay
    requested = 'gpt-shared-long-model-name-' + 'request-variant-' * 5
    response = 'gpt-shared-long-model-name-' + 'response-variant-' * 5
    fixture.call('long-call', configured=requested)
    fixture.observe('long-call', requested=requested, response=response)
    fixture.refresh()
    widget = fixture.widget
    model = widget.content_model
    expected = f'모델 불일치 (요청:{requested}, 응답:{response})'
    assert expected in widget.accessibleName()
    assert widget.toolTip() == expected
    assert fixture.header.toolTip() == expected
    assert ''.join(model.context_rows()) == expected
    top_metrics = QFontMetrics(font(model.appearance.family, 12))
    assert len(model.context_rows()) > 1
    assert all(top_metrics.horizontalAdvance(line) <= 348 for line in model.context_rows())
    assert model.layout()['cache'] >= 48 + len(model.context_rows()) * 18 + 18
    assert model.layout()['height'] >= model.layout()['status'] + 32
    cache_link = next(link for link in model.monitor_links() if link['id'] == 'cache')
    assert cache_link['y'] == model.layout()['cache'] + 16
    item = next(item for item in model.detail_items()
                if (''.join(item[0]) if isinstance(item[0], list) else item[0]) == expected)
    assert isinstance(item[0], list) and len(item[0]) > 1
    metrics = QFontMetrics(font(model.appearance.family, item[5], item[7]))
    assert all(metrics.horizontalAdvance(line) <= item[3] for line in item[0])
    assert item[4] >= metrics.height() * len(item[0])
    assert '…' not in ''.join(item[0])
    detail = fixture.reveal_comparison(expected)
    assert widget.grab().save(str(tmp_path / 'long-model-summary.png'))
    assert detail.grab().save(str(tmp_path / 'long-model-detail.png'))
    assert not widget.qml_errors


@pytest.mark.parametrize('observations,expected', [
    ([dict(requested='gpt-6-astra', response=''), dict(requested='', response='gpt-6-astra')], ''),
    ([dict(), dict()], '모델 일치 (gpt-6-astra)'),
    ([dict(status='pending'), dict(status='completed')], ''),
])
def test_comparison_uses_completed_paired_evidence_not_combined_names(observed_overlay, observations, expected):
    fixture = observed_overlay
    fixture.call('paired-call')
    for index, observation in enumerate(observations):
        fixture.observe('paired-call', attempt=f'pair-{index}', **observation)
    data = fixture.refresh()
    assert not data['latest']['timing_valid']
    assert fixture.widget.content_model.context()[0] == expected
    assert fixture.widget.toolTip() == expected
    summaries = [value for value in detail_values(fixture.widget.content_model) if comparison_lines(value)]
    assert summaries == ([expected] if expected else [])
