"""Present stored own summaries without reading historical call populations."""
from types import SimpleNamespace

from .analysis_records import compact_record
from .overlay_data import OverlaySummaries, call_summary
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
            own = packet['own']
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
        result = self.presenter.present(SimpleNamespace(sessions=sessions), updated,
                                        self.partials, speed_results)
        token = (view.publication_token.instance, view.publication_token.generation)
        return [dict(value, _prepared_token=token, _recent_refs=True) for value in result]


def hydrate_overlay(snapshot, summary, *, health=False):
    """Hydrate a selected display's top twelve refs in per-owner SQL batches."""
    owner = check_reference(snapshot, summary)
    result = dict(summary)
    if summary.get('_recent_refs'):
        refs = summary['recent']
        if len(refs) > MAX_RECENT:
            raise CapacityExceeded('select the twelve recent references before hydration')
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
    if health:
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
