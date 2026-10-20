"""Present stored own summaries without reading historical call populations."""
from types import SimpleNamespace
from collections import OrderedDict
from copy import deepcopy
from contextlib import closing
import sys

from .analysis_records import compact_record
from .overlay_data import OverlaySummaries, call_summary, _subagent_finance, SUBAGENT_CALL_FIELDS
from .prepared_health import check_reference, hydrate_health
from .prepared_store import CapacityExceeded, NotReady, RebuildRequired, _size, pack
from .prepared_summary import recent_references, MAX_RECENT


LATEST_FIELDS = ('model', 'model_setting', 'response_model', 'model_mismatch',
                 'model_state', 'effort', 'mode', 'response_mode', 'transport',
                 'transport_state', 'cache_rate')


class PreparedOverlays:
    """Keep only bounded own heads, refs and existing display rollups per owner."""
    def __init__(self, *, compact=False, finance=False):
        self.compact = compact
        self.finance = finance
        self.presenter = OverlaySummaries()
        self.own = {}
        self.partials = {}
        self.revisions = {}
        self.detail_revisions = {}
        self.summaries = {}

    def _fork(self):
        """Stage map replacements without changing the published projection."""
        candidate = object.__new__(type(self))
        candidate.__dict__ = self.__dict__.copy()
        for name in ('own', 'partials', 'revisions', 'detail_revisions', 'summaries'):
            setattr(candidate, name, getattr(self, name).copy())
        candidate.presenter = object.__new__(type(self.presenter))
        candidate.presenter.__dict__ = self.presenter.__dict__.copy()
        # present replaces these dictionaries and never changes their values.
        return candidate

    def collect(self, view, owners, changed, removed, speed_results, *, bind_generation=True, selected=None):
        visible = {key: value for key, value in owners.items()
                   if not value['metadata'].get('_internal_review')}
        changed = set(visible) if changed is None else set(changed)
        for key in set(self.own).difference(visible) | set(removed):
            self.own.pop(key, None)
            self.partials.pop(key, None)
            self.revisions.pop(key, None)
        for key in visible:
            if key not in changed and key in self.own:
                continue
            if self.revisions.get(key)==(view.publication_token.instance,view.publication_token.generation):
                continue
            packet = view.derived(*key, 'summary')
            if packet is None:
                raise NotReady('the displayed owner has no published summary')
            self.prime(view,key,packet)
        sessions = {key: dict(source=value['metadata'], prepared=value['metadata'])
                    for key, value in visible.items()}
        updated = {}
        for key in sessions:
            prior = self.presenter.cache.get(key)
            updated[key] = (prior if prior and prior[0] == self.revisions[key]
                            and prior[1] is self.own[key] else (self.revisions[key], self.own[key]))
        previous = self.presenter.presented
        result = self.presenter.present(SimpleNamespace(sessions=sessions), updated,
                                        self.partials, speed_results, include_finance=self.finance,
                                        compact=self.compact,selected=selected)
        token = (view.publication_token.instance, view.publication_token.generation)
        revisions = {}
        for key, current in self.presenter.presented.items():
            prior = previous.get(key)
            # A rollup is replaced for any changed member or ancestry. Reuse
            # its proof without retaining a second descendant dependency list.
            revisions[key] = (self.detail_revisions[key] if prior and prior[0] is current[0]
                              else token)
        self.detail_revisions = revisions
        summaries = {}
        for value in result:
            key = (value['home'], value['id'])
            prior = previous.get(key)
            summaries[key] = (self.summaries[key] if prior and prior[2] is value
                              else dict(value, _recent_refs=True, _overlay_revision=revisions[key],
                                        **({'_overlay_head': True} if self.compact else {})))
        self.summaries = summaries
        # Publication keeps content stable across generations. Direct callers
        # still get exact-generation references suitable for hydration.
        return ([dict(value, _prepared_token=token) for value in summaries.values()]
                if bind_generation else list(summaries.values()))

    def prime(self,view,key,packet):
        """Retain only overlay scalars from an already decoded publication packet."""
        refs=list(recent_references(packet))
        own={name:value for name,value in packet['own'].items()
             if name!='model_breakdown' or self.finance}
        if self.compact:
            # Only the latest reference is needed for the global status head.
            # Compositions and the graph's twenty calls belong to selection.
            refs=refs[-1:]
            for name in ('token_composition','cost_composition'):
                own.pop(name,None)
        if refs:
            refs[-1]=dict(refs[-1],**{name:own.get(name) for name in LATEST_FIELDS},cost=own.get('latest_cost'))
        self.own[key]=dict(own,recent=refs,latest=refs[-1] if refs else {})
        self.partials[key]=packet['summary']['partials']
        self.revisions[key]=(view.publication_token.instance,view.publication_token.generation)


FINANCIAL_FIELDS = ('home', 'id', 'title', 'cost', 'calls', 'priced', 'missing',
    'coverage_gap', 'assumed', 'model_breakdown', 'token_composition', 'cost_composition') + SUBAGENT_CALL_FIELDS


def _financial_projection(packet, member):
    if packet is None:
        raise NotReady('a displayed financial member has no summary')
    recent_references(packet)
    own = packet['own']
    if (own['home'], own['id']) != member:
        raise RebuildRequired('financial summary belongs to another owner')
    value = {name: own[name] for name in FINANCIAL_FIELDS}
    value['request'] = {'state': own.get('request', {}).get('state')}
    return value


class FinanceCache:
    """Actor-private, version-bound member projections, never complete replies."""
    MAX_BYTES = 8 * 1024 * 1024
    MAX_ENTRIES = 2048

    def __init__(self):
        self.entries = OrderedDict()
        self.content_bytes = 0
        self.instance = None

    @property
    def bytes(self):
        return (sys.getsizeof(self) + sys.getsizeof(self.__dict__) +
                sum(sys.getsizeof(name) for name in self.__dict__) +
                sys.getsizeof(self.entries) + sys.getsizeof(self.content_bytes) +
                sys.getsizeof(self.instance) + self.content_bytes)

    def clear(self):
        self.entries.clear()
        self.content_bytes = 0

    def _trim(self, limit, extra=0):
        budget = min(self.MAX_BYTES, max(0, limit-extra))
        while self.entries and (len(self.entries) > self.MAX_ENTRIES or self.bytes > budget):
            _, (_, charge) = self.entries.popitem(last=False)
            self.content_bytes -= charge

    def _put(self, key, value, limit, extra):
        # The key, value, wrapper tuple and recorded charge are all retained.
        charge = _size((key, value)) + sys.getsizeof(0)
        prior = self.entries.pop(key, None)
        if prior is not None:
            self.content_bytes -= prior[1]
        self.entries[key] = (value, charge)
        self.content_bytes += charge
        self._trim(limit, extra)

    def _workspace(self, limit, extra):
        self._trim(limit, extra)
        if self.bytes + extra > limit:
            raise CapacityExceeded('selected financial detail exceeds its shared object budget')

    def _members(self, snapshot, members, versions, owner, *, batch):
        for start in range(0, len(members), batch):
            selected = members[start:start+batch]
            misses = []
            for member in selected:
                slot = self.entries.get((member,versions[member]))
                if slot is None or member != owner and slot[0][1] is None:
                    misses.append(member)
            if len(misses) <= 1:
                for member in selected: yield member, None
                continue
            with snapshot.member_packets(misses, 'finance') as packets:
                for member in selected:
                    if member not in misses:
                        yield member, None
                        continue
                    row = next(packets, None)
                    if row is None or row[:2] != (member, versions[member]):
                        raise NotReady('financial miss does not match its committed revision')
                    yield member, row[2]
                    del row

    def collect(self, snapshot, summary, owner, members, *, reserved=0):
        try:
            return self._collect(snapshot,summary,owner,members,reserved=reserved,batch=True)
        except CapacityExceeded:
            if snapshot.closed: raise
            # Optional SQL workspace must not make a previously admissible
            # family fail. Drop the traceback before retrying narrow reads.
            pass
        return self._collect(snapshot,summary,owner,members,reserved=reserved,batch=False)

    def _collect(self, snapshot, summary, owner, members, *, reserved, batch):
        snapshot.check()
        if self.instance != snapshot.token.instance:
            self.clear()
            self.instance = snapshot.token.instance
        versions = {}
        limit = snapshot.limits.page_objects_bytes
        header_bytes = _size(versions)
        # Keep selected SQL parameter inventories bounded independently of the
        # family's size. Every requested member must have a current public head.
        for start in range(0, len(members), 128):
            selected = members[start:start+128]
            after = None
            while True:
                page = snapshot.owner_revisions(selected, after=after)
                for row in page.rows:
                    member = row['owner']
                    if member in versions or member not in selected:
                        raise RebuildRequired('financial version inventory repeats an owner')
                    versions[member] = row['revision']
                    header_bytes += _size((member, row['revision'])) + 256
                    self._workspace(limit, reserved + header_bytes + page.object_bytes)
                complete, next_after = page.complete, page.after
                # Revision pages are transient admission inputs. Do not retain
                # their row dictionaries alongside later decoded member misses.
                del page
                row = None
                if complete:
                    break
                after = next_after
        if versions.keys() != set(members):
            raise NotReady('a displayed financial member is absent')
        parents = {};financial = [];size = reserved + _size((versions, parents, financial))
        # The optional query workspace must not exclude a family that fits
        # through individual reads under a deliberately small object budget.
        batch = 16 if batch and limit-size-self.bytes >= 8192 else 1
        workspace = 8192 if batch > 1 else 0
        with closing(self._members(snapshot,members,versions,owner,batch=batch)) as packets:
            for member, packet in packets:
                snapshot.check()
                packet_workspace = _size(packet) if packet is not None else 0
                key = (member, versions[member])
                slot = self.entries.get(key)
                value = slot[0] if slot is not None else None
                needs_projection = member != owner
                if value is None or needs_projection and value[1] is None:
                    if packet is None: packet = snapshot.financial_member(*member)
                    if packet is None:
                        raise NotReady('a displayed financial member is absent')
                    # A decoded miss coexists with already copied result members.
                    packet_size = _size(packet)
                    self._workspace(limit, size + packet_size + workspace)
                    metadata = packet['metadata']
                    if metadata.get('_internal_review'):
                        raise RebuildRequired('an internal owner cannot contribute displayed finances')
                    projection = _financial_projection(packet['summary'], member) if needs_projection else None
                    value = ((member[0], metadata.get('parent_thread_id')), projection,
                             _size(projection) if projection is not None else 0)
                    # Unpack owns these values. Nothing nested escapes this cache;
                    # projection copies below establish the only mutable boundary.
                    self._put(key, value, limit, size + packet_size + workspace)
                    del packet, metadata, projection
                elif slot is not None:
                    self.entries.move_to_end(key)
                parents[member] = value[0]
                size += _size((member, value[0])) + 256
                if needs_projection:
                    projection_size = value[2]
                    size += projection_size
                    if size > limit:
                        raise CapacityExceeded('selected financial detail exceeds the delivered object budget')
                    self._workspace(limit, size + projection_size + workspace + packet_workspace)
                    financial.append(deepcopy(value[1]))
                self._workspace(limit, size + workspace + packet_workspace)
        _financial_ancestry(parents, owner, members, snapshot.check)
        snapshot.check()
        result = _subagent_finance(financial, summary['child_cost'])
        self._workspace(limit, size + _size(result))
        snapshot.check()
        return result


def _financial_members(summary, owner):
    members = summary.get('members', (owner,))
    if (not isinstance(members, (list, tuple)) or not members or
            any(not isinstance(member, (list, tuple)) or len(member) != 2 or
                any(type(value) is not str for value in member) for member in members)):
        raise RebuildRequired('invalid displayed financial membership')
    members = tuple(map(tuple, members))
    if (members[0] != owner or len(set(members)) != len(members) or
            any(member[0] != owner[0] for member in members) or
            len(members) != summary.get('descendants', 0) + 1):
        raise RebuildRequired('displayed financial membership differs from its owner')
    return members


def _financial_ancestry(parents, owner, members, check):
    resolved = {owner}
    for member in members[1:]:
        check()
        seen = set()
        while member not in resolved:
            check()
            if member in seen or member not in parents:
                raise RebuildRequired('a displayed financial member has unrelated ancestry')
            seen.add(member)
            member = parents[member]
        resolved.update(seen)


def _hydrate_finance(snapshot, summary, owner, cache=None, *, reserved=0):
    """Read descendant financial packets only, in the displayed generation."""
    members = _financial_members(summary, owner)
    if len(members) == 1:
        return None
    if (cache is not None and snapshot.ready and snapshot.staged_batch is None
            and summary.get('_prepared_token') is not None):
        return cache.collect(snapshot, summary, owner, members, reserved=reserved)
    parents = {}
    for member in members:
        entry = snapshot.owner(*member)
        if entry is None:
            raise NotReady('a displayed financial member is absent')
        metadata = entry['metadata']
        if metadata.get('_internal_review'):
            raise RebuildRequired('an internal owner cannot contribute displayed finances')
        parents[member] = (member[0], metadata.get('parent_thread_id'))
    _financial_ancestry(parents, owner, members, snapshot.check)
    financial = []
    size = 0
    for member in members[1:]:
        packet = snapshot.derived(*member, 'summary')
        value = _financial_projection(packet, member)
        size += _size(value)
        if size > snapshot.limits.page_objects_bytes:
            raise CapacityExceeded('selected financial detail exceeds the delivered object budget')
        financial.append(value)
    return _subagent_finance(financial, summary['child_cost'])


def hydrate_overlay(snapshot, summary, *, finance=False, health=False, finance_cache=None):
    """Hydrate recent refs; add financial and health detail only on demand."""
    owner = check_reference(snapshot, summary)
    if summary.get('_overlay_head'):
        owners={}
        parents={}
        members=_financial_members(summary,owner)
        size=0
        for member in members:
            key=tuple(member)
            entry=snapshot.owner(*key)
            if entry is None:
                raise NotReady('selected overlay member is no longer available')
            owners[key]={'metadata':entry['metadata']}
            if entry['metadata'].get('_internal_review'):
                raise RebuildRequired('an internal owner cannot contribute displayed usage')
            parents[key]=(key[0],entry['metadata'].get('parent_thread_id'))
            size+=_size(owners[key])+_size(key)
            if size>snapshot.limits.page_objects_bytes:
                raise CapacityExceeded('selected overlay family exceeds its object budget')
        _financial_ancestry(parents,owner,members,snapshot.check)
        presenter=PreparedOverlays(finance=finance)
        for key in owners:
            packet=snapshot.derived(*key,'summary')
            if packet is None:
                raise NotReady('selected overlay member has no summary')
            size+=_size(packet)
            if finance_cache is not None:
                finance_cache._workspace(snapshot.limits.page_objects_bytes,size)
            if size>snapshot.limits.page_objects_bytes:
                raise CapacityExceeded('selected overlay family exceeds its object budget')
            presenter.prime(snapshot,key,packet)
        packet=None
        values=presenter.collect(snapshot,owners,None,(),{owner:summary.get('speed_health',{})},selected={owner})
        selected=next(value for value in values if (value['home'],value['id'])==owner)
        summary=dict(selected,_overlay_revision=summary['_overlay_revision'])
        del presenter,values,owners,selected
    result = dict(summary)
    if summary.get('_recent_refs'):
        refs = summary['recent']
        if len(refs) > MAX_RECENT:
            raise CapacityExceeded('select the twenty recent references before hydration')
        groups = {}
        for ref in refs:
            groups.setdefault((ref['home'], ref['sid']), []).append(ref)
        facts = {}
        for member, selected in groups.items():
            after = None
            while True:
                page = snapshot.records(member, [ref['key'] for ref in selected], after=after)
                for row in page.rows:
                    facts[(member, pack(row['key'], snapshot.limits))] = row
                if page.complete:
                    break
                after = page.after
        recent = []
        size = 0
        for ref in refs:
            key = ((ref['home'], ref['sid']), pack(ref['key'], snapshot.limits))
            row = facts.get(key)
            if row is None:
                raise NotReady('a displayed summary references an absent call')
            if type(row.get('ts')) is not type(ref['ts']) or row['ts'] != ref['ts']:
                raise RebuildRequired('a displayed summary references different call facts')
            value = call_summary(compact_record(row), ref['ordinal'],
                                 miss=ref['cache_miss'], degradation=ref['cache_degradation'])
            if type(value['id']) is not type(ref['id']) or value['id'] != ref['id']:
                raise RebuildRequired('a displayed summary references a different call identity')
            if 'source_title' in ref:
                value['source_title'] = ref['source_title']
            size += _size(value)
            if size > snapshot.limits.page_objects_bytes:
                raise CapacityExceeded('recent call summaries exceed the delivered object budget')
            recent.append(value)
        result.update(recent=recent, latest=recent[-1] if recent else {}, _recent_refs=False)
        # Only the projected recent rows belong to the reply. Release decoded
        # record pages and lookup tables before admitting financial cache work.
        facts.clear()
        groups.clear()
        del facts, groups
        page = row = selected = ref = value = key = member = refs = None
    if finance and 'subagent_finance' not in result:
        # The recent projection already exists while financial packets are
        # decoded and copied. Reserve it once throughout that working admission.
        reserved = _size(result) if finance_cache is not None else 0
        financial = _hydrate_finance(snapshot, summary, owner, finance_cache, reserved=reserved)
        if financial is not None:
            result['subagent_finance'] = financial
    if health and not result.get('_health_detail'):
        result.update(hydrate_health(snapshot, owner))
        result['_health_detail'] = True
    result_size = _size(result)
    if result_size > snapshot.limits.page_objects_bytes:
        raise CapacityExceeded('selected overlay exceeds the delivered object budget')
    if finance_cache is not None:
        finance_cache._workspace(snapshot.limits.page_objects_bytes, result_size)
    snapshot.check()
    return result


def health_detail(summary, resolver):
    """Consumer hook: resolve only selected detail, never a collapsed overlay."""
    if not summary or not summary.get('_health_ref') or summary.get('_health_detail'):
        return summary
    if resolver is None:
        raise NotReady('selected overlay health detail has not been requested')
    return resolver(summary, health=True)
