"""Rebuild a separate ledger; never replace or modify the supplied evidence."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.quota_cycles import QuotaLedger
from cachemonitor.index import UsageIndex
from cachemonitor.core import Monitor
from cachemonitor.analysis_engine import AnalysisEngine


def rebuild(source,index_path,destination):
    if destination.exists():raise ValueError('Destination already exists')
    old=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
    new=QuotaLedger(destination)
    recovered={}
    # Rowid ranges isolate damaged pages without trusting damaged secondary indexes.
    for table in ('observations','observation_context','window_observations','manual_resets'):
        total=0;lost=[]
        try:maximum=old.execute('select max(rowid) from '+table).fetchone()[0] or 0
        except sqlite3.DatabaseError:
            recovered[table]={'read':0,'unreadable_table':True};continue
        def read(lo,hi):
            nonlocal total
            try:rows=old.execute('select * from '+table+' not indexed where rowid between ? and ?',(lo,hi)).fetchall()
            except sqlite3.DatabaseError:
                if lo==hi:lost.append(lo);return
                mid=(lo+hi)//2;read(lo,mid);read(mid+1,hi);return
            for row in rows:
                new.db.execute('insert or ignore into '+table+' values('+','.join('?' for _ in row)+')',row)
                total+=1
        for lo in range(1,maximum+1,256):read(lo,min(lo+255,maximum))
        recovered[table]={'read':total,'unreadable_rowids':len(lost)}
    new.db.commit();old.close()
    index=UsageIndex.__new__(UsageIndex)
    index.db=sqlite3.connect(index_path.resolve().as_uri()+'?mode=ro',uri=True)
    index.metadata={(h,s):json.loads(d) for h,s,d in index.db.execute('select home,tid,data from metadata')}
    index.monitor=Monitor([]);index.loaded=set();index.turn_states={}
    index.new_events=defaultdict(list);index.event_tails={};index.rebuild_required=set()
    now=time.time()
    for key in index.db.execute('select distinct home,tid from events').fetchall():index.rebuild(key,now)
    sessions=[s.view(now) for s in index.monitor.sessions.values() if s.requests and not s.excluded_title]
    homes=sorted({s['home'] for s in sessions})
    snapshot={'sessions':sessions,'homes':homes,'ts':now,'index':{'loading':False}}
    new.enrich_modes(snapshot)
    engine=AnalysisEngine();engine.ingest(sessions);new.sync(engine,snapshot)
    for home in homes:new.import_observations(index_path,home)
    assert new.db.execute('pragma integrity_check').fetchone()[0]=='ok'
    counts={table:new.db.execute('select count(*) from '+table).fetchone()[0] for table in recovered}
    new.close();index.db.close()
    return {'recovered':recovered,'result':counts,'sessions':len(sessions)}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('source','index','destination'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(rebuild(args.source,args.index,args.destination),ensure_ascii=False))
