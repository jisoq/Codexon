"""Prepared analysis writer and independent, bounded query delivery.

The process connection belongs to the dispatcher. The writer owns collection,
ledger and prepared-store connections; the reader actor owns its own read-only
connections. Neither may send on the GUI connection from a background thread.
"""
from multiprocessing import Pipe
from multiprocessing.connection import wait
from dataclasses import replace
from pathlib import Path
from threading import Condition, Event, Thread
import sqlite3
import time
import traceback

from .analysis_engine import retained_bytes
from .analysis_delivery import ANALYSIS_IDLE_WAIT_SECONDS
from .analysis_query_actor import QueryActor
from .prepared_ingest import ModeEvidenceUnavailable,PreparedCohort, read_catalog
from .prepared_ledger import PreparedLedger
from .prepared_query import GenerationJournal,PreparedQuery, token_value, tuple_token
from .prepared_store import Limits,PreparedStore
from .quota_cycles import QuotaLedger
from .usage_collection import CollectionClient, CollectionCatalogUnavailable


class Mailbox:
    """One retained publication, with backpressure instead of lost deltas."""
    def __init__(self, stopped):
        self.stopped = stopped
        self.condition = Condition()
        self.receiver, self.signal = Pipe(duplex=False)
        self.value = None

    def put(self, value):
        if retained_bytes(value, 64*1024*1024) > 64*1024*1024:
            raise ValueError('prepared publication exceeds its delivery budget')
        with self.condition:
            self.condition.wait_for(lambda:self.value is None or self.stopped.is_set())
            if self.stopped.is_set():
                return False
            self.value = value
            self.signal.send_bytes(b'1')
            return True

    def take(self):
        with self.condition:
            self.receiver.recv_bytes()
            value, self.value = self.value, None
            self.condition.notify_all()
            return value

    def stop(self):
        with self.condition:
            self.condition.notify_all()

    def close(self):
        self.receiver.close()
        self.signal.close()


class Controls:
    """Coalesced commands wake a sleeping collector without a polling thread."""
    def __init__(self):
        self.condition = Condition()
        self.pending = set()
        self.receiver, self.signal = Pipe(duplex=False)

    def put(self, name):
        with self.condition:
            first = not self.pending
            self.pending.add(name)
            if first:
                self.signal.send_bytes(b'1')

    def take(self):
        with self.condition:
            self.receiver.recv_bytes()
            pending, self.pending = self.pending, set()
            return pending

    def close(self):
        self.receiver.close()
        self.signal.close()


class PreparedWriter:
    def __init__(self, homes, index_path, model_evidence_path, quota_path,cache_floor=None):
        self.arguments = homes,index_path,model_evidence_path,quota_path
        self.path = Path(quota_path).with_suffix('.prepared.sqlite')
        self.stopped = Event()
        self.frozen = Event()
        self.cache_floor=time.time() if cache_floor is None else cache_floor
        self.output = Mailbox(self.stopped)
        self.control = Controls()
        self.thread = Thread(target=self._run, name='prepared-writer', daemon=True)
        self.thread.start()

    def freeze(self):
        self.frozen.set()
        self.control.put('freeze')

    def sync(self):
        self.control.put('sync')

    def set_cache_floor(self,value):
        import math
        if type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError('notification floor must be a finite timestamp')
        self.cache_floor=value

    def _run(self):
        collector = ledger = store = speed_writer = None
        try:
            from .prepared_delivery import PreparedPublisher
            homes,index_path,model_evidence_path,quota_path = self.arguments
            collector = CollectionClient(homes,index_path,model_evidence_path)
            store = PreparedStore(self.path,replace(Limits(),read_seconds=60,read_vm_steps=20_000_000))
            from .prepared_speed_writer import PreparedSpeedWriter
            from .prepared_ingest import preparation_signature
            speed_writer=PreparedSpeedWriter(store,preparation=preparation_signature(),
                                              cancel=self.stopped,read_seconds=60)
            publisher = PreparedPublisher(self.cache_floor)
            adapter = None
            snapshot = None
            catalog = None
            notification = None
            imported = False
            ledger_issue = None
            failures = 0
            ledger_retry = next_poll = next_import = 0.
            last_timing = float('-inf')
            delivery_delta=None
            pending_changed=set();pending_removed=set();ledger_removed=set()
            maintenance_due=True
            ledger_pending=False
            pending_reset=False

            def maintain():
                deadline=time.monotonic()+.02
                while not self.stopped.is_set():
                    if not store.maintain():return False
                    if time.monotonic()>=deadline:return True
                return True

            def timing(stages):
                nonlocal last_timing
                from .quota_diagnostics import record_slow_cycle
                age=max(0,time.time()-snapshot['ts']) if snapshot is not None else 0
                last_timing=record_slow_cycle(Path(quota_path).with_suffix('.analysis.sqlite'),
                                              stages,age,last_timing)

            def disable(error):
                nonlocal ledger,adapter,ledger_issue,ledger_retry,failures
                from .quota_diagnostics import record_failure,storage_issue
                record_failure(quota_path,'usage ledger sync',error)
                ledger_issue=storage_issue(error)
                failures+=1
                ledger_retry=time.monotonic()+min(60,5*2**min(failures-1,4))
                if ledger is not None:
                    try:ledger.db.rollback()
                    except sqlite3.Error:pass
                    try:ledger.close()
                    except sqlite3.Error:pass
                ledger=adapter=None

            def publish(*, reset=False):
                nonlocal delivery_delta,pending_reset
                pending_reset=pending_reset or reset
                if (snapshot is None or publisher.token!=store.token or
                        not store.db.execute('SELECT ready FROM meta WHERE singleton=1').fetchone()[0]):
                    return
                if pending_reset:
                    publisher.reset()
                value=dict(snapshot,ledger_status_known=(imported and not ledger_pending) or ledger is None,
                           ledger_fault=dict(ledger_failed=bool(ledger_issue)))
                if ledger_issue:
                    value['ledger_error']=ledger_issue
                else:
                    value.pop('ledger_error',None)
                # Publication never reads retained calls. Its summaries and
                # choices are prepared by the source writer's committed cohort.
                light=publisher.publish(value,store,speed_writer.tracker.results)
                self.output.put(dict(kind='snapshot',value=light,token=tuple_token(store.token),
                                     transition=delivery_delta))
                delivery_delta=None
                pending_reset=False

            def import_step():
                nonlocal imported,next_import,ledger_issue,failures
                started=time.monotonic()
                try:
                    imported=ledger.import_observations_step(collector.path,snapshot['homes'])
                except sqlite3.Error as error:
                    disable(error)
                if ledger is not None and imported:
                    ledger_issue=None;failures=0
                next_import=time.monotonic()+.05
                timing(dict(history_import=time.monotonic()-started))

            while not self.stopped.is_set():
                if self.control.receiver.poll():
                    commands=self.control.take()
                    if 'stop' in commands:
                        break
                    if 'freeze' in commands:
                        collector.stop_notifications();notification=None
                    if 'sync' in commands:
                        publish(reset=True)
                incomplete=(bool(pending_changed or pending_removed) or
                    store.token.generation>0 and not store.db.execute(
                        'SELECT ready FROM meta WHERE singleton=1').fetchone()[0]) if self.frozen.is_set() else False
                if self.frozen.is_set() and not incomplete:
                    if maintenance_due:
                        maintenance_due=maintain()
                    wait([self.control.receiver],timeout=.05 if maintenance_due else None)
                    continue
                now=time.monotonic()
                if ledger is None and now>=ledger_retry:
                    try:
                        ledger=QuotaLedger(quota_path)
                        adapter=PreparedLedger(ledger,store)
                        cursor=ledger.activity_cursor()
                        if cursor:collector.channel.activity_ack=cursor
                        next_poll=0
                    except sqlite3.Error as error:
                        disable(error)
                if now>=next_poll:
                    started=time.monotonic()
                    try:
                        catalog=read_catalog(collector,cancel=self.stopped.is_set)
                        # An incomplete live membership cannot withdraw owners
                        # or replace committed analytical state.
                        if not catalog['membership_complete']:
                            if snapshot is not None:
                                # Read freshness belongs to the current source
                                # header, not the saved analytical generation.
                                # Keep committed owners and replay acknowledgement
                                # while making stale/loading/error state visible.
                                header=catalog['header']
                                snapshot={**snapshot,**{key:value for key,value in header.items()
                                    if key not in ('sessions','request_activity','activity_cursor')}}
                                if 'collection_stale' not in header:
                                    snapshot.pop('collection_stale',None)
                                publish()
                            next_poll=time.monotonic()+(3 if snapshot is not None else .1)
                        else:
                            if ledger is not None:
                                try:ledger.scan_modes(catalog['selected_homes'])
                                except sqlite3.Error as error:disable(error)
                            coordinator=PreparedCohort(store,collector,ledger,
                                cancel=self.stopped.is_set,speed_writer=speed_writer)
                            for change in coordinator.convert(catalog):
                                pending_removed.difference_update(change['changed'])
                                pending_changed.update(change['changed'])
                                pending_changed.difference_update(change['removed'])
                                pending_removed.update(change['removed'])
                                ledger_removed.difference_update(change['changed'])
                                ledger_removed.update(change['removed'])
                                maintenance_due=True
                                if self.stopped.is_set():
                                    raise InterruptedError('prepared conversion cancelled')
                                if not change['ready']:
                                    # Keep a large one-time conversion's cleanup
                                    # debt within one admitted cohort.
                                    while maintain():
                                        if self.stopped.is_set():
                                            raise InterruptedError('prepared conversion cleanup cancelled')
                                    maintenance_due=False
                            coordinator.current_speeds()
                            previous=publisher.token
                            publisher.adopt(store,pending_changed,pending_removed)
                            if previous!=store.token:
                                delivery_delta=(tuple_token(previous) if previous else None,
                                                tuple_token(store.token),tuple(pending_changed),tuple(pending_removed))
                            publisher.cache_floor=self.cache_floor
                            snapshot=collector.channel.activity_page(dict(catalog['header']),catalog['publication'][0])
                            ledger_pending=ledger is not None
                            if previous!=store.token:
                                # A verified display need not await independent
                                # accounting replay. Its pending status is explicit.
                                publish()
                            pending_changed.clear();pending_removed.clear()
                            if ledger is not None:
                                try:
                                    for _ in adapter.sync(snapshot,removed=ledger_removed):
                                        if self.stopped.is_set():
                                            raise InterruptedError('prepared ledger interrupted between durable batches')
                                    if snapshot.get('activity_cursor'):
                                        collector.channel.acknowledge_activity(snapshot)
                                    ledger_removed.clear()
                                    if imported:
                                        ledger_issue=None;failures=0
                                except sqlite3.Error as error:
                                    disable(error)
                            ledger_pending=False
                            publish()
                            timing(dict(prepared_update=time.monotonic()-started))
                            notification=collector.notification_connection(catalog['publication'])
                            age=time.time()-snapshot['ts']
                            delay=.1 if snapshot['index'].get('loading') else (
                                3 if notification is None or age>30 else min(ANALYSIS_IDLE_WAIT_SECONDS,max(.01,30-age+.01)))
                            next_poll=time.monotonic()+delay
                    except CollectionCatalogUnavailable:
                        # AppServices owns collector startup and restart. The
                        # query writer never starts a duplicate source reader.
                        if snapshot is not None and time.time()-snapshot['ts']>30 and not snapshot.get('collection_stale'):
                            snapshot=dict(snapshot,collection_stale=True,usage_collection_complete=False,
                                          index={**snapshot['index'],'usage_complete':False})
                            publish()
                        next_poll=time.monotonic()+(3 if snapshot is not None else .1)
                    except ModeEvidenceUnavailable as error:
                        disable(error.__cause__)
                        next_poll=0
                    except InterruptedError:
                        if self.stopped.is_set():break
                        raise
                if (ledger is not None and snapshot is not None and not imported
                        and not snapshot['index'].get('loading') and time.monotonic()>=next_import):
                    import_step()
                    if imported or ledger is None:publish()
                if maintenance_due:
                    maintenance_due=maintain()
                delay=max(0,next_poll-time.monotonic())
                if maintenance_due:delay=min(delay,.05)
                if ledger is None:
                    delay=min(delay,max(0,ledger_retry-time.monotonic()))
                elif snapshot is not None and not imported and not snapshot['index'].get('loading'):
                    delay=min(delay,max(0,next_import-time.monotonic()))
                handles=[self.control.receiver]
                if notification is not None:handles.append(notification)
                ready=wait(handles,timeout=delay)
                if notification is not None and notification in ready:
                    notice=collector.consume_notification(catalog['publication'])
                    if notice is True:next_poll=0
                    elif notice is None:next_poll=min(next_poll,time.monotonic()+3)
                    notification=collector.notification_connection(catalog['publication']) if notice is False else None
        except Exception:
            self.output.put(dict(kind='fatal',error=traceback.format_exc(limit=6)))
        finally:
            if collector is not None:collector.close()
            if ledger is not None:ledger.close()
            if speed_writer is not None:speed_writer.close()
            if store is not None:store.close()

    def close(self):
        self.stopped.set()
        self.control.put('stop')
        self.output.stop()
        # Cancellation rolls back a live preparation; completed ledger batches
        # retain their durable source certificates for the next start.
        self.thread.join()
        self.output.close()
        self.control.close()


def process_main(connection,homes,index_path,model_evidence_path,quota_path,cache_floor=None):
    """Return an explicit replacement snapshot for the static compatibility path."""
    writer=PreparedWriter(homes,index_path,model_evidence_path,quota_path,cache_floor)
    actor=QueryActor(lambda:PreparedQuery(writer.path))
    current=None
    pending={}
    latest_query=None
    blocked_query_token=None
    journal=GenerationJournal()
    try:
        while True:
            ready=wait([connection,writer.output.receiver,actor.notification])
            if connection in ready:
                while connection.poll():
                    message=connection.recv();kind=message['kind']
                    if kind=='stop':return None
                    if kind=='replace':return message['snapshot']
                    if kind=='freeze':writer.freeze()
                    elif kind=='sync':writer.sync()
                    elif kind=='cache_floor':writer.set_cache_floor(message['value'])
                    elif kind in ('query','record','population','overlay_detail'):
                        pending[kind]=message
                        if kind=='query':
                            latest_query=message;blocked_query_token=None
            if writer.output.receiver in ready:
                message=writer.output.take()
                if message['kind']=='fatal':
                    connection.send(message);return None
                current=token_value(message.pop('token'))
                journal.add(message.pop('transition'))
                connection.send(message)
            if actor.notification in ready:
                for message in actor.drain():
                    if message['kind']=='stale' and message['consumer']=='query':
                        if latest_query and latest_query['id']==message['id']:
                            pending['query']=latest_query
                            blocked_query_token=tuple(message['prepared_token'])
                        continue
                    connection.send(message)
                    if message['kind']=='fatal':return None
            if current is not None:
                for kind,message in tuple(pending.items()):
                    if kind=='query' and blocked_query_token==tuple_token(current):continue
                    requested=(message.get('prepared_token') if kind=='record' else
                               message['summary'].get('_prepared_token') if kind=='overlay_detail' else None)
                    token=token_value(requested) if requested is not None else current
                    actor.submit(dict(message,_prepared_changes=journal.values()),token)
                    pending.pop(kind,None)
    finally:
        try:actor.close()
        finally:writer.close()
