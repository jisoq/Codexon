"""Version-bound operations executed exclusively by the read actor."""
from pathlib import Path
import sqlite3
from .analysis_engine import retained_bytes
from .prepared_analysis import PreparedAnalysisEngine
from .prepared_store import PreparedReader, Token, StaleGeneration, NotReady, CapacityExceeded


def token_value(value):
    if isinstance(value,Token):
        return value
    if (not isinstance(value,(tuple,list)) or len(value)!=2 or type(value[0]) is not str
            or type(value[1]) is not int or value[1]<0):
        raise ValueError('query requires an exact prepared generation')
    return Token(*value)


class _Cancellation:
    def __init__(self,callback):self.callback=callback
    def is_set(self):return self.callback()


class PreparedQuery:
    """One actor owns reader lifetime and its bounded analytical result caches."""
    def __init__(self,path):
        from .prepared_overlays import FinanceCache
        self.path=path
        self.engine=None
        from .mismatch_history import MismatchReader
        archive_name=Path(path).name.removesuffix('.prepared.sqlite')+'.mismatches.sqlite'
        self.mismatches=MismatchReader(Path(path).with_name(archive_name))
        self.finance_cache=FinanceCache()

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
        try:
            return self._call(message,token,cancel)
        except CapacityExceeded:
            # Drop reproducible, lower-priority results after the read scope
            # unwinds. A second failure belongs only to this selected request.
            self.finance_cache.clear()
            if self.engine is not None:
                self.engine.close()
                self.engine=None
        return self._call(message,token,cancel)

    def _call(self,message,token,cancel):
        if message['kind'] in ('mismatch_page','mismatch_record'):
            kind=message['kind']
            try:
                result=(self.mismatches.window(start=message.get('start',0),size=message.get('size',128),cancel=cancel)
                        if kind=='mismatch_page' else self.mismatches.record(message['identity'],cancel=cancel))
                return dict(kind=kind,id=message['id'],**result)
            except (OSError,ValueError,sqlite3.Error) as error:
                return dict(kind=kind,id=message['id'],error=str(error))
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
                    value=hydrate_overlay(view,summary,finance=bool(message.get('finance')),
                                          finance_cache=self.finance_cache)
                return dict(kind=kind,id=message['id'],summary=value,prepared_token=tuple_token(token))
            if kind!='query':
                raise ValueError('unknown prepared query operation')
            if retained_bytes(message['query'],4*1024*1024)>4*1024*1024:
                raise ValueError('page query exceeds its request budget')
            query=dict(message['query'],_prepared_token=tuple_token(token))
            progress=message.get('_progress')
            if progress:progress('query')
            engine=self._engine(token,cancellation,message.get('_prepared_changes',()))
            engine.progress=progress
            try:
                result=engine.page_query(query)
                valid_until=result['valid_until']
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
        self.mismatches.close()
        self.finance_cache.clear()
        if self.engine is not None:
            self.engine.close();self.engine=None


def tuple_token(token):
    return (token.instance,token.generation)
