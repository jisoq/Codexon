"""Temporal cost shares, synchronized inspection, and accent-bounded UI colors."""
import math
from pathlib import Path
import pytest
from PySide6.QtCore import QObject, Qt, QPointF
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from cachemonitor.quota_view import prepare_series
from cachemonitor.quota_share import prepare_model_share, share_at
from cachemonitor.quota_chart import QuotaHistory
from cachemonitor.quick_qa import mount, render_plot, dispose, walk
from cachemonitor.overlay_appearance import Appearance, CodexAppearance
from cachemonitor.token_colors import ui_palette, oklab, TOKEN_ROLES, contrast_ratio
from cachemonitor.overlay_view import palette
from cachemonitor.theme import shared_theme

SEEDS=[('#212121','#eeffff','#80cbc4'),('#f6f3ed','#322a25','#a05b22'),
       ('#101c2c','#e3eefc','#82aaff'),('#f4f0f9','#322740','#8654bc')]

def series_fixture():
    times=list(range(1200,2161,60));cost=0;rows=[];events=[]
    additions={1260:('astra',2),1320:('sol',6),1560:('astra',9),1620:('sol',1)}
    for at in times:
        if at in additions:
            name,amount=additions[at];cost+=amount
            events.append(dict(ts=at-10,model=name,cost=amount,reasoning='high'))
        rows.append(dict(at=at,remaining=100-(at-1200)//120,cycle_cost=cost,cycle_value=500,
                         cycle_start=1200,connect=bool(rows),reset_kind=None,label=str(at)))
    intervals=[dict(start=1200,end=2160,separate_models=['separate'],cost_rows=events+[
        dict(ts=1280,model='separate',cost=999)])]
    series=prepare_series(rows);series['model_share']=prepare_model_share(series,intervals)
    return series,intervals

def test_five_minute_shares_follow_recorded_cost_increments_not_lifetime_totals():
    series,intervals=series_fixture()
    assert share_at(series,1440)['shares']=={'astra':25,'sol':75}
    assert share_at(series,1680)['shares']=={'astra':90,'sol':10}
    assert share_at(series,1860)['state']=='zero'
    assert all(math.isclose(sum(b['shares'].values()),100) for b in series['model_share']['bins'].values() if b['state']=='cost')
    assert 'separate' not in series['model_share']['models']
    # Unpriced evidence must not be silently normalized with known-cost shares.
    intervals[0]['cost_rows'].append(dict(ts=1580,model='astra',cost=None))
    series['model_share']=prepare_model_share(series,intervals)
    assert share_at(series,1680)['state']=='unknown' and not share_at(series,1680)['shares']
    assert share_at(series,1860)['state']=='zero'
    # A corrected total with no corresponding model evidence remains unknown.
    series['rows'][2]['cycle_cost']+=3
    series['model_share']=prepare_model_share(series,intervals)
    assert share_at(series,1380)['state']=='unknown'

@pytest.mark.parametrize('surface,ink,accent',SEEDS)
def test_accent_bounded_palette_and_shared_roles(surface,ink,accent):
    appearance=Appearance(dark=surface in ('#212121','#101c2c'),surface=surface,ink=ink,accent=accent)
    p=ui_palette(appearance);overlay=palette(appearance)
    lightness,a,b=oklab(accent);chroma=math.hypot(a,b)
    for key in TOKEN_ROLES:
        l,c,d=oklab(p[key])
        assert abs(l-lightness)<=.017
        assert math.hypot(c,d)<=chroma*1.02
        assert overlay[key].name()==p[key]
        assert all(contrast_ratio(p[key],bg)>=3 for bg in (p['surface'],p['overlay']))
        assert all(contrast_ratio(p[key+'_text'],bg)>=4.5 for bg in (p['surface'],p['overlay']))
    assert p['accent']==accent
    for key in ('ink','muted','success','warning','error'):
        assert contrast_ratio(p[key],p['surface'])>=4.5

@pytest.mark.parametrize('width',[520,1120])
def test_chart_and_strip_share_cursor_annotations_pin_escape_and_theme(tmp_path,width):
    app=QApplication.instance() or QApplication([])
    theme=shared_theme();theme.configure(appearance=Appearance(surface=SEEDS[0][0],ink=SEEDS[0][1],accent=SEEDS[0][2]))
    series,_=series_fixture();chart=QuotaHistory();chart.money=True;chart.set_series(series)
    host=mount(chart,width,720)
    try:
        plot=render_plot(host,chart)
        plot.findChild(QObject,'plotHover').setProperty('enabled',False)
        x=chart.x_at_time(1650)
        plot.showTip(x,chart.strip_box.center().y());QTest.qWait(40)
        assert chart.inspection_x==pytest.approx(x)
        assert plot.detail['at']==1620  # Step value, never the next observation.
        assert {r['label']:r['value'] for r in plot.detail['share']['items']}=={'astra':'90.0%','sol':'10.0%'}
        from_strip=plot.detail
        plot.showTip(x,chart.box.center().y());QTest.qWait(20)
        assert plot.detail==from_strip
        cards={item.objectName():item for item in walk(host.quick.quickWindow().contentItem())
               if item.objectName() in ('quotaDetailCard','quotaShareDetailCard') and item.isVisible()}
        assert len(cards)==2
        primary=cards['quotaDetailCard'];share=cards['quotaShareDetailCard']
        assert share.y()>=primary.y()+primary.height()
        assert share.y()+share.height()<=host.quick.height()
        assert host.grab().save(str(tmp_path/f'share-hover-{width}.png'))
        plot.activateAt(x,chart.strip_box.center().y());plot.clearHover()
        assert plot.detailPinned and chart.inspection_x is not None
        identity=theme.model_color('astra');theme.register_models(['sol','earlier-alphabetic-model'])
        assert theme.model_color('astra')==identity
        theme.configure(appearance=Appearance(False,*SEEDS[1]))
        QTest.qWait(40);host.quick.grabFramebuffer()
        assert plot.detailPinned and plot.detail['inspection_at']==pytest.approx(1650)
        assert theme.model_color('astra')!=identity
        plot.key(Qt.Key_Right);assert plot.detail['share']
        plot.key(Qt.Key_Escape);assert not plot.detail and chart.inspection_x is None
        assert not host.qml_errors,host.qml_errors
    finally:dispose(host)

def test_last_valid_theme_survives_temporarily_missing_file(tmp_path):
    config=tmp_path/'config.toml'
    config.write_text('[desktop]\nappearanceTheme="dark"\n[desktop.appearanceDarkChromeTheme]\nsurface="#212121"\nink="#eeffff"\naccent="#80cbc4"\n')
    reader=CodexAppearance(config);original=reader.read(True);config.unlink()
    assert reader.read(True)==original

def test_ui_color_sources_have_no_literal_palette():
    root=Path(__file__).resolve().parents[1]/'cachemonitor'
    import re
    for p in [*root.glob('*.py'),*root.glob('qml/*.qml')]:
        if p.name=='connection_recovery.py':continue  # Independent emergency recovery program.
        assert not re.search(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b',p.read_text(encoding='utf-8-sig')),p
