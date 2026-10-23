from collections import deque
from hashlib import sha256
import math
import sqlite3
import zlib

from .app_errors import exception_facts
from .collection_lifecycle import collector_identity
from .observer_state import read_json
from .usage_collection import CollectionHeaderReader,CollectionPending


class CollectionObserver:
    def __init__(self,homes,index=None,evidence=None):
        self.reader=CollectionHeaderReader(homes,index,evidence)
        self.source=None;self.snapshot=None;self.retired=deque(maxlen=4)

    def observe(self):
        facts={}
        try:
            snapshot=self.reader.read_header()
            if snapshot is not None:self.snapshot=snapshot
            if self.snapshot is not None:
                stamp=self.snapshot.get('ts')
                collection=self.snapshot.get('collection') or {}
                if (not isinstance(stamp,(int,float)) or not math.isfinite(stamp)
                        or not isinstance(collection.get('instance'),str) or not collection['instance']):
                    raise ValueError('Invalid collector publication')
                source=collector_identity(self.reader,self.snapshot,self.source)
                if (source is None or collection['instance'] in self.retired
                        or self.source and source['instance']==self.source['instance'] and source['process']!=self.source['process']):
                    self.snapshot=None
                else:
                    if self.source and source['instance']!=self.source['instance']:self.retired.append(self.source['instance'])
                    self.source=source
        except CollectionPending:
            self.snapshot=None
        except (OSError,ValueError,RuntimeError,TypeError,KeyError,sqlite3.Error,zlib.error) as error:
            facts=dict(status_failed=True,**exception_facts(error))
            self.source=None;self.snapshot=None
        stamp=self.snapshot['ts'] if self.snapshot is not None else None
        marker=read_json(self.reader.companion('.collector-error.json'))
        if (marker.get('scope')==sha256(self.reader.scope.encode('utf-8')).hexdigest()
                and isinstance(marker.get('at'),(int,float)) and marker['at']>(stamp or 0)
                and isinstance(marker.get('facts'),dict)):
            facts.update(marker['facts'])
        return dict(collection_snapshot_at=stamp,collection_fault=facts)

    def close(self):
        self.reader.close()
