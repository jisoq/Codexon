"""Immutable navigation intent shared by the dashboard and external overlay."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class NavigationTarget:
    home: str
    sid: str
    view: str = 'requests'
    request_id: str | None = None
    call_id: str | None = None
    event_id: str | None = None
    filters: tuple[str, ...] = ()
    sort: str = 'time_desc'
    section: str = 'identity'
    tab: str = 'sessions'

    def __post_init__(self):
        object.__setattr__(self, 'filters', tuple(self.filters))

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_value(cls, value):
        return value if isinstance(value, cls) else cls(**value)


def navigation_target(data, kind, *, call=None, event=None, status=None):
    """Resolve the displayed object now; never resolve 'latest' after activation."""
    if not data or not data.get('home') or not data.get('id'):
        return None
    base = dict(home=data['home'], sid=data['id'])
    if kind == 'speed_alert':
        health = data.get('speed_health', {})
        if health.get('active'):
            return NavigationTarget(**base, view='speed_alert',
                                    event_id=str(health['incident_id']), call_id=str(health['call_id']))
        return None
    if kind == 'session':
        return NavigationTarget(**base)
    if kind == 'cost_total':
        return NavigationTarget(**base, view='requests', sort='cost_desc')
    if kind == 'status_total' and status:
        return NavigationTarget(**base, view='calls', filters=(status,))
    if kind == 'collection':
        return NavigationTarget(**base, tab='settings', view='troubleshooting', section='collection')
    if kind == 'incident' and event and event.get('id') is not None:
        return NavigationTarget(**base, view='incident', event_id=str(event['id']), section='evidence')
    call = call if call is not None else data.get('latest')
    identity = (call or {}).get('call_id') or (call or {}).get('id') or (call or {}).get('key')
    if identity is None:
        return None
    section = {'cache': 'usage', 'speed': 'time', 'cost': 'pricing', 'observation': 'evidence', 'call': 'identity'}.get(kind)
    if kind=='speed' and call.get('output_speed') is None:section='usage'
    if section:
        return NavigationTarget(**base, view='calls', call_id=str(identity),
                                request_id=call.get('turn'), section=section)
    return None
