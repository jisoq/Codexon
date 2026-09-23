from pathlib import Path
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from cachemonitor.charts import ComparisonChart, comparison_axis
from cachemonitor.presentation import TextArea, Column
from cachemonitor.ui_details import Details
from cachemonitor.quick_qa import mount, dispose, control, click, walk
from cachemonitor.theme import shared_theme
from cachemonitor.token_colors import contrast_ratio
from cachemonitor.quota_cycles import quota_value_history
from cachemonitor.quota_view import history_rows, prepare_quota_view
from test_quota_attribution import example


def test_confirmed_ratio_survives_next_request_and_updates_on_completion():
    report=example([(100,100),(130,98),(160,98),(190,97),(220,96),(250,95),(280,95)],
                   [('a',110,115,2),('b',175,240,6)])
    rows,_=quota_value_history(report,history_rows(report,'weekly'))
    bytime={r['at']:r for r in rows}
    assert bytime[160]['cycle_value']==100
    assert bytime[190]['value_pending']
    assert bytime[190]['cycle_value']==bytime[220]['cycle_value']==100
    assert bytime[280]['cycle_value']==160
    overall=prepare_quota_view(report)['overall']['rows']
    assert next(r for r in overall if r['at']==220)['cycle_value']==100
    assert overall[-1]['cycle_value']==160


def test_distribution_axis_covers_visible_marks_without_hidden_maximum():
    row=dict(n=100,p10=10,q1=12,median=15,q3=18,p90=20,value=16,maximum=10000)
    lo,hi=comparison_axis([row],'cost')
    assert lo<10<20<hi and hi-lo<15
    lo,hi=comparison_axis([dict(n=2,points=[1,500],value=250)],'cost')
    assert lo<=1 and hi>500
    for value in (0,100):
        lo,hi=comparison_axis([dict(n=1,points=[value],value=value)],'cache_ratio')
        assert lo<=value<=hi and hi>lo


@pytest.mark.parametrize('mode',['light','dark','custom'])
def test_plain_diagnostics_and_box_plot_render_with_theme(tmp_path,mode):
    app=QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration',True)
    theme=shared_theme();theme.configure('dark' if mode=='custom' else mode)
    if mode=='custom':
        theme._palette.update(ink='#202020',secondary='#666666',accent='#880088');theme.changed.emit()
    area=TextArea();area.setPlainText('수집 대상\nC:\\Users\\USER\\.codex\n한도 원장 · 정상')
    details=Details('수집 상세',area)
    plot=ComparisonChart();plot.metric='cost'
    plot.set_rows([dict(id='A',n=100,N=100,p10=10,q1=12,median=15,q3=18,p90=20,value=16,maximum=10000)])
    root=Column();root.addWidget(details);root.addWidget(plot)
    host=mount(root,1000,650)
    try:
        click(host,control(host,details.toggle));QTest.qWait(80)
        editors=[i for i in walk(control(host,area)) if i.metaObject().indexOfProperty('textFormat')>=0]
        editor=next(i for i in editors if i.property('text') is not None)
        assert editor.property('text')==area.text()
        assert '<!DOCTYPE' not in editor.property('text')
        assert contrast_ratio(editor.property('color'),theme.palette['secondary'])>=4.5
        frame=host.quick.grabFramebuffer();assert not frame.isNull()
        output=Path('artifacts/ui-fixes');output.mkdir(parents=True,exist_ok=True)
        assert frame.save(str(output/f'{mode}.png'))
        assert not host.qml_errors
    finally:
        dispose(host);theme.configure('light')
