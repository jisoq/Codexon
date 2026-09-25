"""Explicit, durable permission to take unbounded-output risk, not capability proof.

The existing request journal is the only usage ledger. Reservations serialize
all permitted sessions; only the pre-body permit consumes a call. No retries.
"""
import hashlib
import json
import math
import time
import uuid
from urllib.parse import urlsplit


def maintenance_route(url, websocket):
    """Select the independent transport, never change the user's connection.

    Only the exact Codex endpoint may cross from a captured WebSocket to HTTP.
    Contexts must already have reconstructed its complete response chain.
    This is a wire conversion, not proof of cross-transport cache retention.
    """
    endpoint=urlsplit(url)
    if (endpoint.scheme in ('https','wss') and endpoint.netloc=='chatgpt.com' and
        endpoint.path=='/backend-api/codex/responses' and not endpoint.query and not endpoint.fragment):
        return endpoint._replace(scheme='https').geturl(),False
    return url,websocket


def target(home, request, url, headers, websocket,*,observed_tier=None):
    endpoint=urlsplit(url)
    if observed_tier=='Standard':observed_tier='default'
    account=next((v for k,v in headers.items() if k.lower()=='chatgpt-account-id'),None)
    # Scope identification is not authorization or a claim of model support.
    if (endpoint.scheme!='https' or endpoint.netloc!='chatgpt.com' or endpoint.query or endpoint.fragment or
        endpoint.path!='/backend-api/codex/responses' or websocket or not isinstance(account,str) or not account or
        not isinstance(request.get('model'),str) or not request['model'] or
        not isinstance((request.get('reasoning') or {}).get('effort'),str) or
        (request.get('service_tier') or observed_tier)!='default'):return None
    return dict(home=str(home),account=hashlib.sha256(account.encode()).hexdigest(),
                model=request['model'],effort=request['reasoning']['effort'],tier='default',transport='http',
                endpoint='https://chatgpt.com/backend-api/codex/responses')


class Operations:
    def __init__(self,journal):
        self.journal=journal;self.db=journal.db

    def propose(self,scope,expected,adverse,output_high,basis,*,cost_stop=None,purpose='maintenance',dynamic_estimate=False):
        if not scope or not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in (expected,adverse,output_high)):return None
        if purpose not in ('maintenance','diagnostic'):raise ValueError('invalid_purpose')
        if cost_stop is not None and (not isinstance(cost_stop,(int,float)) or not math.isfinite(cost_stop) or cost_stop<=0):raise ValueError('invalid_cost_stop')
        # These are consent defaults, not inferred optimal limits or cost caps.
        proposal=dict(scope=scope,duration=3600,max_calls=2,cost_stop=2*expected if cost_stop is None else cost_stop,
                      purpose=purpose,dynamic_estimate=dynamic_estimate,
                      expected=expected,adverse=adverse,output_high=output_high,basis=basis,
                      server_output_limit='unsupported_in_tested_http_request')
        key=hashlib.sha256(json.dumps(proposal,sort_keys=True).encode()).hexdigest()
        self.db.execute('DELETE FROM cache_operating_proposals WHERE at<?',(time.time()-1800,))
        self.db.execute('INSERT OR REPLACE INTO cache_operating_proposals VALUES(?,?,?,?)',
                        (key,scope['home'],json.dumps(proposal),time.time()))
        return dict(proposal,id=key)

    def proposals(self,home):
        return [dict(json.loads(data),id=key) for key,data in self.db.execute(
            'SELECT id,data FROM cache_operating_proposals WHERE home=? AND at>? ORDER BY at DESC,rowid DESC',
            (home,time.time()-1800))]

    def grants(self,home=None):
        return [dict(json.loads(data),id=key,created=created,expires=expires,stopped=stopped)
                for key,data,created,expires,stopped in self.db.execute(
                    'SELECT id,data,created,expires,stopped FROM cache_operating_grants ORDER BY created DESC,rowid DESC')
                if home is None or json.loads(data)['scope']['home']==home]

    def stats(self,grant):
        rows=[r for r in self.journal.rows() if r.get('operation')==grant['id']]
        known=[r['cost'] for r in rows if r['cost'] is not None]
        return dict(calls=len(rows),remaining=max(0,grant['max_calls']-len(rows)),
                    observed=sum(known) if known or not rows else None,
                    unknown=sum(r['cost'] is None for r in rows))

    def consent(self,proposal_id):
        """Called only for explicit authorization of this exact scope."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            record=self.db.execute('SELECT data,at FROM cache_operating_proposals WHERE id=?',(proposal_id,)).fetchone()
            if not record or record[1]<time.time()-1800:raise ValueError('proposal_expired')
            proposal=json.loads(record[0])
            if any(r['operation'] and r['cost'] is None for r in self.journal.rows()):raise ValueError('usage_unresolved')
            for old in self.grants():
                stats=self.stats(old)
                if stats['unknown']:raise ValueError('usage_unresolved')
                if not old['stopped'] and old['expires']>time.time():raise ValueError('existing_permission')
            key=uuid.uuid4().hex;now=time.time()
            self.db.execute('INSERT INTO cache_operating_grants VALUES(?,?,?,?,?,NULL)',
                            (key,proposal['scope']['home'],json.dumps(proposal),now,now+proposal['duration']))
            self.db.execute('COMMIT');return key
        except BaseException:
            self.db.execute('ROLLBACK');raise

    def stop(self,key,reason):
        self.db.execute('UPDATE cache_operating_grants SET stopped=COALESCE(stopped,?) WHERE id=?',(reason,key))

    def permission(self,scope):
        return next((g for g in self.grants() if g['scope']==scope),None)

    def check(self,operation,exclude=None):
        grant=next((g for g in self.grants() if g['id']==operation['id']),None)
        if not grant or grant['scope']!=operation['scope']:return 'scope_mismatch'
        if grant['stopped']:return grant['stopped']
        if operation.get('purpose','maintenance')!=grant.get('purpose','maintenance'):return 'purpose_mismatch'
        if not all(type(operation.get(k)) in (int,float) and math.isfinite(operation[k]) and operation[k]>0
                   for k in ('expected','adverse','output_high')):return 'operating_cost_unobserved'
        if self.db.execute("SELECT 1 FROM cache_jobs WHERE operation IS NOT NULL AND state IN ('reserved','sent') AND id!=?",
                           (exclude or '',)).fetchone():return 'operation_busy'
        reason=None;stats=self.stats(grant)
        if grant['expires']<=time.time():reason='permission_expired'
        elif any(r['operation'] and r['cost'] is None for r in self.journal.rows()):reason='usage_unresolved'
        elif stats['calls']>=grant['max_calls']:reason='call_limit'
        elif stats['observed']>=grant['cost_stop']:reason='observed_cost_stop'
        elif not grant.get('dynamic_estimate') and (operation['expected']>grant['expected'] or operation['adverse']>grant['adverse']):reason='scope_cost_increased'
        elif stats['observed']+operation['expected']>grant['cost_stop']:return 'projected_cost_stop'
        if reason:self.stop(grant['id'],reason);return reason
        # One shared in-flight reservation, including other homes and grants.
        return None

    def reconcile(self,key):
        record=self.db.execute('SELECT operation,expected_cost,adverse_cost,output_high,read_required FROM cache_jobs WHERE id=?',(key,)).fetchone()
        if not record or not record[0]:return
        grant=next(g for g in self.grants() if g['id']==record[0])
        row=next((r for r in self.journal.rows() if r['job_id']==key),None)
        if row is None:return  # Cancelled before body; consumes no call.
        stats=self.stats(grant)
        reason=('usage_unresolved' if row['cost'] is None else
                'request_failed' if row['state']!='completed' else
                'observed_cost_stop' if stats['observed']>=grant['cost_stop'] else
                'cost_above_estimate' if row['cost']>record[1] else
                'output_above_observed' if row['output']>record[3] else
                'reuse_unconfirmed' if not row['scope_read_lower'] else
                'partial_reuse' if row['scope_read_lower']<(record[4] or 0) else
                'call_limit' if stats['calls']>=grant['max_calls'] else None)
        if reason:self.stop(grant['id'],reason)
