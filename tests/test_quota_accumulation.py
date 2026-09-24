from cachemonitor.quota_cycles import split_cycles,quota_statistics
from test_quota_integrity import observe,sync
from cachemonitor.quota_cycles import QuotaLedger


def observations():
    return [dict(id=str(i),at=100+i*100,used=10+i,reset=10000,minutes=10080,
                 account='a',source='live',plan='pro',bucket='codex',separate='[]') for i in range(5)]


def test_source_duplicates_unknown_identity_and_input_order_do_not_split():
    rows=observations()
    additions=[{**r,'id':'copy'+r['id'],'source':'local'} for r in rows]
    additions += [{**r,'id':'unknown'+r['id'],'account':'','source':'history'} for r in rows]
    for incoming in (rows,rows+additions,list(reversed(rows+additions))):
        groups=split_cycles(incoming)
        assert len(groups)==1
        assert groups[0]['observations'][-1]['used']-groups[0]['observations'][0]['used']==4


def test_selected_period_uses_matching_endpoints_and_calls(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        for at,used in [(100,10),(200,12),(300,14),(400,16)]:observe(ledger,at,used)
        sync(ledger,[150,250,350])
        stats=quota_statistics(ledger.report('h',1000),180,350)
        assert stats['valid']==1 and stats['delta']==2 and stats['calls']==1
        assert stats['intervals'][0]['start']==200 and stats['intervals'][0]['end']==300
        early=ledger.report('h',250)
        assert max(r['end'] for r in early['cycles'])<=250
    finally:ledger.close()
