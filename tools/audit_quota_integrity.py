"""Read-only report of the active local ledger, optionally render its panel."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cachemonitor.quota_cycles import QuotaLedger, quota_statistics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--rebuild-index', type=Path,
                        help='Reconstruct coverage in memory from the sanitized usage index')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    if args.rebuild_index:
        from collections import defaultdict
        from types import SimpleNamespace
        from cachemonitor.core import Session
        memory=sqlite3.connect(':memory:')
        db.backup(memory);db.close();db=memory;db.row_factory=sqlite3.Row
        db.executescript('''CREATE TABLE IF NOT EXISTS coverage_issues(
            home TEXT,sid TEXT,start REAL,end REAL,reason TEXT,PRIMARY KEY(home,sid,reason));
            CREATE TABLE IF NOT EXISTS observation_context(id TEXT PRIMARY KEY,plan TEXT,bucket TEXT);''')
        source=sqlite3.connect(args.rebuild_index.resolve().as_uri()+'?mode=ro',uri=True)
        events=defaultdict(list)
        for home,tid,data in source.execute('select home,tid,data from events order by home,tid,ts,path,pos'):
            events[(home,tid)].append(json.loads(data))
        source.close()
        views=[]
        for (home,tid),records in events.items():
            session=Session(tid,home)
            session.modern=any(e['type']=='token_usage_record' and e['payload'].get('thread_id',tid)==tid for e in records)
            for event in records:session.consume(event,time.time())
            views.append(session.view(time.time()))
        staged=QuotaLedger.__new__(QuotaLedger);staged.db=db
        for state in list(db.execute('select * from state')):
            staged.sync(SimpleNamespace(sessions={}), {'homes':[state['home']],
                'sessions':[v for v in views if v['home']==state['home']],
                'ts':state['at'],'index':{'loading':bool(state['loading'])}})
            staged.import_observations(args.rebuild_index,state['home'])
        db.execute('PRAGMA query_only=ON')
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger.db = db
    home = db.execute('select home from state order by at desc limit 1').fetchone()[0]
    start = time.perf_counter()
    report = ledger.report(home)
    stats = quota_statistics(report)
    evidence = {
        'read_only':True, 'report_seconds':time.perf_counter()-start,
        'counts':{table:db.execute('select count(*) from '+table).fetchone()[0]
                  for table in ('observations','calls','manual_resets')},
        'summary':{k:v for k,v in stats.items() if k not in ('intervals','distribution')},
        'intervals':[{k:r.get(k) for k in ('start','end','delta','cost','per_percent',
                     'calls','reason','status','excluded','assumptions','rejected_observations',
                     'mode_fast_cost','discontinuous','sensitivity')}
                     for r in stats['intervals']],
    }
    db.close()
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.render:
        from PySide6.QtCore import Qt, QSettings, QTimer
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication
        from cachemonitor.fonts import configure_font_rendering, configure_high_dpi, load_bundled_fonts
        from cachemonitor.quota_panel import QuotaPanel
        from cachemonitor.dashboard import STYLE
        configure_font_rendering()
        configure_high_dpi()
        app = QApplication([])
        load_bundled_fonts()
        app.setStyle('Fusion')
        font = QFont('Pretendard JP');font.setPixelSize(14);app.setFont(font)
        app.setStyleSheet(STYLE)
        panel = QuotaPanel(QSettings(str(args.output.with_suffix('.ini')), QSettings.IniFormat))
        panel.receive({'report':report})
        from cachemonitor.quick_runtime import QuickHost
        host=QuickHost();host.setCentralWidget(panel);host.resize(1400,850);host.show()
        def finish():
            host.grab().save(str(args.output.with_suffix('.png')))
            host.release_scene();host.close();app.quit()
        QTimer.singleShot(500,finish)
        app.exec()


if __name__ == '__main__':
    main()
