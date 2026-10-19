"""Present stored own summaries without reading historical call populations."""
from types import SimpleNamespace

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
    def __init__(self):
        self.presenter = OverlaySummaries()
        self.own = {}
        self.partials = {}
        self.revisions = {}
        self.detail_revisions = {}

    def collect(self, view, owners, changed, removed, speed_results):
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
            packet = view.derived(*key, 'summary')
            if packet is None:
                raise NotReady('the displayed owner has no published summary')
            refs = list(recent_references(packet))
            # Detailed accounting stays stored once per owner. Whole-session
            # publication retains only the fields the main overlay displays.
            own = {name: value for name, value in packet['own'].items()
                   if name != 'model_breakdown'}
            # An ancestor's winning reference is the contributing member's
            # latest call. These already computed scalars preserve its headline.
            if refs:
                refs[-1] = dict(refs[-1], **{name: own.get(name) for name in LATEST_FIELDS},
                                cost=own.get('latest_cost'))
            self.own[key] = dict(own, recent=refs, latest=refs[-1] if refs else {})
            self.partials[key] = packet['summary']['partials']
            self.revisions[key] = (view.publication_token.instance, view.publication_token.generation)
        sessions = {key: dict(source=value['metadata'], prepared=value['metadata'])
                    for key, value in visible.items()}
        updated = {key: (self.revisions[key], self.own[key]) for key in sessions}
        previous = self.presenter.presented
        result = self.presenter.present(SimpleNamespace(sessions=sessions), updated,
                                        self.partials, speed_results, include_finance=False)
        token = (view.publication_token.instance, view.publication_token.generation)
        revisions = {}
        for key, current in self.presenter.presented.items():
            prior = previous.get(key)
            # A rollup is replaced for any changed member or ancestry. Reuse
            # its proof without retaining a second descendant dependency list.
            revisions[key] = (self.detail_revisions[key] if prior and prior[0] is current[0]
                              else token)
        self.detail_revisions = revisions
        return [dict(value, _prepared_token=token, _recent_refs=True,
                     _overlay_revision=revisions[(value['home'], value['id'])]) for value in result]


def _hydrate_finance(snapshot, summary, owner):
    """Read descendant financial packets only, in the displayed generation."""
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
    if len(members) == 1:
        return None
    parents = {}
    for member in members:
        entry = snapshot.owner(*member)
        if entry is None:
            raise NotReady('a displayed financial member is absent')
        metadata = entry['metadata']
        if metadata.get('_internal_review'):
            raise RebuildRequired('an internal owner cannot contribute displayed finances')
        parents[member] = (member[0], metadata.get('parent_thread_id'))
    resolved = {owner}
    for member in members[1:]:
        seen = set()
        while member not in resolved:
            if member in seen or member not in parents:
                raise RebuildRequired('a displayed financial member has unrelated ancestry')
            seen.add(member)
            member = parents[member]
        resolved.update(seen)
    financial = []
    size = 0
    fields = ('home', 'id', 'title', 'cost', 'calls', 'priced', 'missing',
              'coverage_gap', 'assumed', 'model_breakdown', 'token_composition', 'cost_composition') + SUBAGENT_CALL_FIELDS
    for member in members[1:]:
        packet = snapshot.derived(*member, 'summary')
        if packet is None:
            raise NotReady('a displayed financial member has no summary')
        # Validate the stored packet without hydrating any historical calls.
        recent_references(packet)
        own = packet['own']
        if (own['home'], own['id']) != member:
            raise RebuildRequired('financial summary belongs to another owner')
        value = {name: own[name] for name in fields}
        value['request'] = {'state': own.get('request', {}).get('state')}
        size += _size(value)
        if size > snapshot.limits.page_objects_bytes:
            raise CapacityExceeded('selected financial detail exceeds the delivered object budget')
        financial.append(value)
    return _subagent_finance(financial, summary['child_cost'])


def hydrate_overlay(snapshot, summary, *, finance=False, health=False):
    """Hydrate recent refs; add financial and health detail only on demand."""
    owner = check_reference(snapshot, summary)
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
    if finance and 'subagent_finance' not in result:
        financial = _hydrate_finance(snapshot, summary, owner)
        if financial is not None:
            result['subagent_finance'] = financial
    if health and not result.get('_health_detail'):
        result.update(hydrate_health(snapshot, owner))
        result['_health_detail'] = True
    if _size(result) > snapshot.limits.page_objects_bytes:
        raise CapacityExceeded('selected overlay exceeds the delivered object budget')
    return result


def health_detail(summary, resolver):
    """Consumer hook: resolve only selected detail, never a collapsed overlay."""
    if not summary or not summary.get('_health_ref') or summary.get('_health_detail'):
        return summary
    if resolver is None:
        raise NotReady('selected overlay health detail has not been requested')
    return resolver(summary, health=True)
