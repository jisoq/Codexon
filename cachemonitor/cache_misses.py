"""Session-wide cache-read misses; classification precedes all view filters."""


def classify(rows):
    # Collection already deduplicates response identities. Preserve its stable
    # order for equal timestamps and never use the first row of a filtered view.
    ordered=sorted(rows,key=lambda r:r['ts'])
    first=ordered[0] if ordered else None
    events=[{'key':r.get('key'),'ts':r['ts'],'input':r.get('input')}
            for r in ordered if type(r.get('input')) is int and r['input']>0 and type(r.get('cached')) is int and r['cached']==0]
    known=[e['input'] for e in events if e['input'] is not None]
    return {'count':len(events),'input':sum(known),'input_missing':len(events)-len(known),
            'events':events,'first_key':first.get('key') if first else None,
            'current':bool(ordered and events and ordered[-1].get('key')==events[-1]['key'])}
