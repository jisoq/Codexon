"""Local control plane. Only numeric usage and hook identities are persisted."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from .model_evidence import default_path
from .pricing import token_cost


def control_path(index_path=None):
    return Path(index_path).with_name('cache-control.sqlite') if index_path else default_path().with_name('cache-control.sqlite')


class Control:
    def __init__(self,path):
        if str(path)!=':memory:':
            path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(str(path),timeout=.5,isolation_level=None)
        self.db.executescript('''PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS cache_preferences(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS cache_profiles(home TEXT,sid TEXT,model TEXT,data TEXT,PRIMARY KEY(home,sid,model));
          CREATE TABLE IF NOT EXISTS cache_inputs(home TEXT,sid TEXT,turn TEXT,at REAL,model TEXT,kind TEXT,PRIMARY KEY(home,sid,turn,kind));
          CREATE TABLE IF NOT EXISTS cache_tickets(id TEXT PRIMARY KEY,home TEXT,sid TEXT,turn TEXT,model TEXT,digest TEXT,created REAL,expires REAL,state TEXT);
          CREATE TABLE IF NOT EXISTS cache_status(home TEXT,sid TEXT,data TEXT,PRIMARY KEY(home,sid));
          CREATE TABLE IF NOT EXISTS cache_gaps(home TEXT,sid TEXT,turn TEXT,data TEXT,PRIMARY KEY(home,sid,turn));
        ''')

    def get(self,key,default=None):
        row=self.db.execute('SELECT value FROM cache_preferences WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO cache_preferences VALUES(?,?)',(key,json.dumps(value)))

    def profile(self,home,sid,row):
        keep=('key','ts','model','effort','service_tier','input','cached','written','output','reasoning','cost','purpose')
        clean={k:row.get(k) for k in keep}
        if clean.get('purpose')=='maintenance':return
        old=self.db.execute('SELECT data FROM cache_profiles WHERE home=? AND sid=? AND model=?',(home,sid,row.get('model') or '')).fetchone()
        if old and json.loads(old[0])==clean:return
        self.db.execute('INSERT OR REPLACE INTO cache_profiles VALUES(?,?,?,?)',(home,sid,row.get('model') or '',json.dumps(clean)))

    def latest(self,home,sid):
        rows=[json.loads(r[0]) for r in self.db.execute('SELECT data FROM cache_profiles WHERE home=? AND sid=?',(home,sid))]
        return max(rows,key=lambda r:r.get('ts') or 0) if rows else None

    def revision(self,home):
        return self.db.execute("SELECT COALESCE(MAX(rowid),0) FROM cache_inputs WHERE home=? AND kind='UserPromptSubmit'",(home,)).fetchone()[0]

    def activity(self,home,event):
        kind=event.get('hook_event_name');sid=event.get('session_id');turn=event.get('turn_id')
        if not isinstance(sid,str) or not sid:return
        # A submission is not proof of a human. Keep this distinction in history.
        self.db.execute('INSERT OR IGNORE INTO cache_inputs VALUES(?,?,?,?,?,?)',
                        (home,sid,str(turn or uuid.uuid4().hex),time.time(),str(event.get('model') or ''),str(kind)))

    def status(self,home,sid,value):
        self.db.execute('INSERT OR REPLACE INTO cache_status VALUES(?,?,?)',(home,sid,json.dumps(value)))

    def requests(self):
        now=time.time()
        self.db.execute("UPDATE cache_tickets SET state='timeout' WHERE state='waiting' AND expires<=?",(now,))
        return [dict(zip(('id','home','sid','turn','model','created','expires','state'),r)) for r in self.db.execute(
            "SELECT id,home,sid,turn,model,created,expires,state FROM cache_tickets WHERE state='waiting' ORDER BY created")]

    def resolve(self,ticket,choice):
        if choice not in ('approve','cancel','dismiss'):return False
        return self.db.execute("UPDATE cache_tickets SET state=? WHERE id=? AND home=? AND sid=? AND turn=? AND model=? AND state='waiting' AND expires>?",
            (choice,ticket['id'],ticket['home'],ticket['sid'],ticket['turn'],ticket['model'],time.time())).rowcount==1

    def guard_needed(self,home,event):
        previous=self.latest(home,event['session_id'])
        if not previous or not previous.get('model') or previous['model']==event.get('model'):return False
        if time.time()-previous['ts']>=1800 or not previous.get('cached'):return False
        target=self.db.execute('SELECT data FROM cache_profiles WHERE home=? AND sid=? AND model=?',
                               (home,event['session_id'],event.get('model'))).fetchone()
        if target and time.time()-json.loads(target[0])['ts']<1800:return False
        # Compare a same-sized cold-input scenario, not destruction of the old cache.
        before=token_cost(previous)['cost']
        after=token_cost({**previous,'model':event.get('model'),'cached':0,
                          'written':previous['input'] if previous.get('written') is not None else None})['cost']
        return before is not None and after is not None and after-before>before

    def close(self):self.db.close()


def hook_decision(path,home,event,timeout=60):
    control=Control(path)
    key=None
    try:
        control.activity(home,event)
        if event.get('hook_event_name')!='UserPromptSubmit':return {}
        if not all(isinstance(event.get(k),str) and event[k] for k in ('session_id','turn_id','model')):return {}
        if not control.get('guard',False) or time.time()-control.get('ui_heartbeat',0)>5:return {}
        if not control.guard_needed(home,event):return {}
        key=uuid.uuid4().hex;now=time.time()
        digest=hashlib.sha256(str(event.get('prompt','')).encode()).hexdigest()
        # One live invocation can be released only once. A repeated hook for an
        # already handled turn never inherits approval or retransmits it.
        control.db.execute('BEGIN IMMEDIATE')
        if control.db.execute('SELECT 1 FROM cache_tickets WHERE home=? AND sid=? AND turn=?',
                              (home,event['session_id'],event['turn_id'])).fetchone():
            control.db.execute('ROLLBACK')
            return {'continue':False,'stopReason':'Codexon: invocation already handled.'}
        control.db.execute('INSERT INTO cache_tickets VALUES(?,?,?,?,?,?,?,?,?)',
            (key,home,event['session_id'],event['turn_id'],event['model'],digest,now,now+timeout,'waiting'))
        control.db.execute('COMMIT')
        while True:
            state=control.db.execute('SELECT state FROM cache_tickets WHERE id=?',(key,)).fetchone()[0]
            if state=='approve':
                control.db.execute("UPDATE cache_tickets SET state='released' WHERE id=? AND state='approve'",(key,))
                return {}
            if state in ('cancel','dismiss','timeout'):
                return {'continue':False,'stopReason':'Codexon: request not approved.'}
            if state in ('unavailable','released'):return {}
            if time.time()>=now+timeout:
                control.db.execute("UPDATE cache_tickets SET state='timeout' WHERE id=? AND state='waiting'",(key,));continue
            if time.time()-control.get('ui_heartbeat',0)>5:
                control.db.execute("UPDATE cache_tickets SET state='unavailable' WHERE id=? AND state='waiting'",(key,))
                # Infrastructure failure passes; an explicit cancellation above never does.
                continue  # Re-read: a concurrent explicit cancellation wins.
            time.sleep(.05)
    except (sqlite3.Error,OSError):
        # Before a ticket exists, infrastructure failure bypasses the guard.
        # After presentation, an unreadable decision may already be a cancellation.
        # Do not silently reverse it into approval.
        return {'continue':False,'stopReason':'Codexon: confirmation connection lost.'} if key else {}
    finally:control.close()


def hook_main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--database',required=True)
    args=parser.parse_args()
    try:
        event=json.loads(sys.stdin.buffer.read(1024*1024))
        home=str(Path(os.environ.get('CODEX_HOME',Path.home()/'.codex')).resolve())
        result=hook_decision(args.database,home,event)
    except Exception:result={}
    # stdout is a protocol, never a debug or user instruction channel.
    sys.stdout.write(json.dumps(result));sys.stdout.flush()
    return 0
