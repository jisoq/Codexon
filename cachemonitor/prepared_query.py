"""Version-bound operations executed exclusively by the read actor."""
from collections import deque
from .analysis_engine import retained_bytes
from .prepared_analysis import PreparedAnalysisEngine
from .prepared_store import PreparedReader, Token, StaleGeneration, NotReady


def token_value(value):
    if isinstance(value,Token):
        return value
    if (not isinstance(value,(tuple,list)) or len(value)!=2 or type(value[0]) is not str
            or type(value[1]) is not int or value[1]<0):
        raise ValueError('query requires an exact prepared generation')
    return Token(*value)


class GenerationJournal:
    """Bounded owner deltas for a reader that may skip writer publications."""
    def __init__(self):
        self.entries=deque()
        self.bytes=0

    def add(self,value):
        if value is None:return
        size=retained_bytes(value,4*1024*1024)
        if size>4*1024*1024 or len(value[2])+len(value[3])>128:
            self.entries.clear();self.bytes=0
            return
        while self.entries and (len(self.entries)>=16 or self.bytes+size>4*1024*1024):
            self.bytes-=self.entries.popleft()[1]
        self.entries.append((value,size));self.bytes+=size

    def values(self):return tuple(value for value,_ in self.entries)


def _reference_token(reference):
    """A union may combine populations only from the same displayed generation."""
    if retained_bytes(reference,4*1024*1024)>4*1024*1024:
        raise ValueError('population reference exceeds its delivery budget')
    pending=[(reference,0)];tokens=set()
    while pending:
        value,depth=pending.pop()
        if depth>32 or not isinstance(value,dict):
            raise ValueError('invalid population reference')
        if 'union' in value:
            parts=value['union']
            if not isinstance(parts,(list,tuple)):
                raise ValueError('invalid population union')
            pending.extend((part,depth+1) for part in parts)
        else:
            tokens.add(token_value(value.get('query',{}).get('_prepared_token')))
    if len(tokens)!=1:
        raise StaleGeneration('population references belong to different displayed generations')
    return tokens.pop()


class _Cancellation:
    def __init__(self,callback):self.callback=callback
    def is_set(self):return self.callback()


def _fresh_reference(value,token,depth):
    if depth>32 or not isinstance(value,dict):
        raise ValueError('invalid refreshed population reference')
    if 'union' in value:
        return dict(value,union=[_fresh_reference(part,token,depth+1) for part in value['union']])
    return dict(value,query=_fresh_query(value['query'],token,depth+1))


def _fresh_query(query,token,depth=0):
    """A page refresh recomputes its drilldown query on the new generation.

    Exact detail requests never use this rebinding. Only query definitions,
    rather than rows or previously calculated memberships, are carried forward.
    """
    if depth>32:raise ValueError('population reference nesting exceeds its limit')
    result=dict(query,_prepared_token=tuple_token(token))
    explorer=query.get('explorer') or {}
    context=explorer.get('context') or {}
    if context.get('population'):
        result['explorer']=dict(explorer,context=dict(context,
            population=_fresh_reference(context['population'],token,depth+1)))
    return result


class PreparedQuery:
    """One actor owns reader lifetime and its bounded analytical result caches."""
    def __init__(self,path):
        self.path=path
        self.engine=None

    def _engine(self,token,cancel,changes=()):
        if self.engine is not None and self.engine.token!=token:
            at=tuple_token(self.engine.token);changed=set();removed=set()
            for base,target,owners,deleted in changes:
                if base is None or tuple(base)!=at:continue
                changed.update(map(tuple,owners));removed.difference_update(map(tuple,owners))
                removed.update(map(tuple,deleted));changed.difference_update(map(tuple,deleted))
                at=tuple(target)
                if at==tuple_token(token):break
            if at==tuple_token(token) and len(changed|removed)<=128:
                self.engine.cancel=cancel
                self.engine.adopt(token,changed,removed)
            else:
                # A reader that missed the bounded journal reloads only owner
                # metadata. Canonical historical calls remain in the store.
                self.engine.close();self.engine=None
        if self.engine is None:
            self.engine=PreparedAnalysisEngine(self.path,token,cancel=cancel)
        else:
            self.engine.cancel=cancel
        return self.engine

    def __call__(self,message,token,cancel):
        token=token_value(token)
        cancellation=_Cancellation(cancel)
        kind=message['kind']
        try:
            if kind=='overlay_detail':
                from .prepared_overlays import hydrate_overlay
                summary=message['summary']
                if token_value(summary.get('_prepared_token'))!=token:
                    raise StaleGeneration('overlay detail belongs to another displayed generation')
                with PreparedReader(self.path) as reader,reader.pin(token,cancel=cancellation) as view:
                    value=hydrate_overlay(view,summary,health=bool(message.get('health')))
                return dict(kind=kind,id=message['id'],summary=value,prepared_token=tuple_token(token))
            if kind=='record':
                requested=message.get('prepared_token')
                if requested is not None and token_value(requested)!=token:
                    raise StaleGeneration('selected record belongs to another displayed generation')
                # A selected record never waits for all owner metadata to load.
                with PreparedReader(self.path) as reader,reader.pin(token,cancel=cancellation) as view:
                    home,sid,key=message['identity']
                    owner=view.owner(home,sid)
                    internal=owner['metadata'].get('_internal_review') if owner else None
                    if owner is not None and type(internal) is not bool:
                        raise NotReady('selected owner has no canonical classification')
                    row=view.record(home,sid,key) if owner is not None and not internal else None
                return dict(kind='record',id=message['id'],row=row,prepared_token=tuple_token(token))
            if kind=='population':
                for reference in (message['population'],message.get('assumptions')):
                    if reference is not None and _reference_token(reference)!=token:
                        raise StaleGeneration('population no longer matches the displayed generation')
                from .dashboard_views import population_summary
                engine=self._engine(token,cancellation,message.get('_prepared_changes',()))
                with engine.read_scope():
                    value=population_summary(engine,message['population'],message.get('assumptions'))
                return dict(kind='population',id=message['id'],summary=value,prepared_token=tuple_token(token))
            if kind!='query':
                raise ValueError('unknown prepared query operation')
            if retained_bytes(message['query'],4*1024*1024)>4*1024*1024:
                raise ValueError('page query exceeds its request budget')
            query=_fresh_query(message['query'],token)
            progress=message.get('_progress')
            if progress:progress('query')
            engine=self._engine(token,cancellation,message.get('_prepared_changes',()))
            from .analysis_worker import expiry
            engine.progress=progress
            try:
                with engine.read_scope():
                    result=engine.page_query(query)
                    valid_until=result['valid_until'] if 'valid_until' in result else expiry(engine,query)
            finally:
                engine.progress=None
            result=dict(result,prepared_token=tuple_token(token))
            return dict(kind='result',id=message['id'],logical=message['logical'],result=result,
                        valid_until=valid_until,metrics=dict(engine.metrics),prepared_token=tuple_token(token))
        except (StaleGeneration,NotReady):
            # The writer/UI must refresh the displayed projection before
            # issuing another detail request. Never substitute current facts.
            return dict(kind='stale',consumer=kind,id=message['id'],prepared_token=tuple_token(token))

    def close(self):
        if self.engine is not None:
            self.engine.close();self.engine=None


def tuple_token(token):
    return (token.instance,token.generation)
