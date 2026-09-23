"""Read-only desktop route adapter. Activity of a secondary task is never selection."""
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit


ROUTE_MARKER = 'IAB_LIFECYCLE received browser sidebar owner sync'
ACTIVITY_MARKER = 'thread_stream_view_activity_changed'
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')


@dataclass(frozen=True)
class Selection:
    thread_id: str | None
    host: str = 'local'
    window_id: str = ''
    route: str = ''


class RouteLog:
    READ_LIMIT = 2 * 1024 * 1024
    def __init__(self, root, extra_roots=()):
        self.root = Path(root)
        self.roots = tuple(dict.fromkeys((self.root, *(Path(p) for p in extra_roots))))
        self.pid = None
        self.path = None
        self.offset = 0
        self.states = {}
        self.inactive = set()
        self.next_discovery = 0
        self.caught_up = False
        self.partial = b''
        self.skipping = False
        self.identity = None
        self.prefix = b''
        self.modified = None
        self.rollover_seed = None
        self.rollover_seeded = False

    def reset_file(self):
        self.offset = 0
        self.states.clear(); self.inactive.clear()
        self.partial = b''; self.skipping = False
        self.identity = None; self.prefix = b''; self.caught_up = False
        self.modified = None
        self.rollover_seeded = False

    def consume(self, line):
        if ROUTE_MARKER not in line and ACTIVITY_MARKER not in line:
            return
        fields = dict(re.findall(r'\b([A-Za-z]+)=([^\s]+)', line))
        if ROUTE_MARKER in line:
            window = fields.get('windowId')
            if not window: return
            if self.rollover_seeded:
                # The first route in the new file supersedes the last route
                # carried over from the preceding day's file.
                self.states.clear();self.inactive.clear();self.rollover_seeded=False
            raw = fields.get('ownerRoutePath', '')
            try:
                route = urlsplit(raw)
                candidate = route.path.removeprefix('/local/') if route.path.startswith('/local/') else ''
                tid = candidate if UUID.fullmatch(candidate) else None
                host = parse_qs(route.query).get('hostId', ['local'])[0]
            except ValueError:
                tid, host = None, 'local'
            self.states[window] = Selection(tid, host, window, raw)
            self.inactive.discard(window)
        else:
            window = fields.get('rendererWindowId')
            selected = self.states.get(window)
            if selected and selected.thread_id == fields.get('conversationId'):
                if fields.get('active') == 'false': self.inactive.add(window)
                elif fields.get('active') == 'true': self.inactive.discard(window)

    def poll(self, pid, now):
        if self.pid != pid:
            self.pid = pid; self.path = None; self.states.clear(); self.inactive.clear()
            self.offset = 0; self.next_discovery = 0; self.caught_up = False
            self.rollover_seed = None
            self.reset_file()
        if now >= self.next_discovery:
            paths = [path for root in self.roots
                     for path in root.glob(f'*/*/*/codex-desktop-*-{pid}-t0-*.log')]
            # Windows may give two adjacent daily logs the same modification
            # timestamp. The dated path then identifies the newer file.
            path = max(paths, key=lambda p: (p.stat().st_mtime_ns, str(p)), default=None)
            self.next_discovery = now + 2
            if path != self.path:
                self.rollover_seed = self._preceding_day_selection(paths,path,pid)
                self.path = path; self.offset = 0; self.states.clear(); self.inactive.clear()
                self.reset_file()
        if self.path is None: return None
        with self.path.open('rb') as file:
            import os
            stat = os.fstat(file.fileno())
            size = stat.st_size
            identity = (stat.st_dev, stat.st_ino)
            prefix = file.read(128)
            if (self.identity != identity or size < self.offset
                    or size==self.offset and self.modified is not None and stat.st_mtime_ns!=self.modified
                    or self.prefix and not prefix.startswith(self.prefix)):
                self.reset_file()
                if self.rollover_seed:
                    self.states[self.rollover_seed.window_id]=self.rollover_seed
                    self.rollover_seeded=True
                    self.rollover_seed=None
            self.identity = identity; self.prefix = prefix
            self.modified = stat.st_mtime_ns
            file.seek(self.offset)
            # Bound each read; no source-file size can stall the GUI or tracker indefinitely.
            data = file.read(self.READ_LIMIT)
        self.offset += len(data)
        self.caught_up = False
        if self.skipping:
            end = data.find(b'\n')
            if end < 0: return None
            data = data[end+1:]; self.skipping = False
        data = self.partial + data
        self.partial = b''
        pieces = data.split(b'\n')
        for line in pieces[:-1]:
            if len(line) <= self.READ_LIMIT:
                self.consume(line.decode('utf-8', 'replace'))
        tail = pieces[-1]
        if len(tail) >= self.READ_LIMIT:
            self.skipping = True
        else:
            self.partial = tail
        self.caught_up = self.offset >= size and not self.partial and not self.skipping
        if not self.caught_up or len(self.states) != 1: return None
        selected = next(iter(self.states.values()))
        return None if selected.window_id in self.inactive else selected

    def _preceding_day_selection(self, paths, path, pid):
        if path is None:return None
        try:
            current_day=date(*(int(part) for part in path.parts[-4:-1]))
            prefix=path.name.split(f'-{pid}-t0-',1)[0]
            previous=[p for p in paths if p.name!=path.name
                      and p.name.split(f'-{pid}-t0-',1)[0]==prefix
                      and date(*(int(part) for part in p.parts[-4:-1]))==current_day-timedelta(days=1)
                      and 0<=path.stat().st_ctime-p.stat().st_mtime<=120]
            if not previous:return None
            prior=max(previous,key=lambda p:p.stat().st_mtime_ns)
            source=RouteLog(self.root)
            with prior.open('rb') as file:
                size=prior.stat().st_size
                start=max(0,size-self.READ_LIMIT)
                file.seek(start)
                if start:file.readline()
                data=file.read(self.READ_LIMIT)
            for line in data.split(b'\n')[:-1]:source.consume(line.decode('utf-8','replace'))
            if len(source.states)!=1:return None
            selection=next(iter(source.states.values()))
            return None if selection.window_id in source.inactive else selection
        except (OSError,ValueError):
            return None


def overlay_geometry(frame, dpi, width=380, height=212, margin=16, position='bottom-right', anchor=None):
    return anchored_monitor_geometry(frame,dpi,width,height,margin=margin,
                                     position=position,anchor=anchor,edge_anchor=False)


def anchor_for_position(frame, geometry, x, y, dpi, margin=16):
    return monitor_anchor_for_position(frame,geometry,x,y,dpi,
                                       reference_height=geometry[3]*96/dpi,margin=margin,edge_anchor=False)


def anchored_monitor_geometry(frame, dpi, width, height, *, reference_width=None, reference_height=None,
                              margin=16, position='bottom-right', anchor=None, edge_anchor=True):
    """Place logical content in native coordinates, preserving its lower right anchor.

    Saved edge fractions are independent of conditional rows, reduced content,
    details and the restore icon. Only this boundary applies OS scaling. Legacy
    free-space anchors remain available for migration and existing callers.
    """
    factor = dpi / 96
    gap = round(margin * factor)
    left, top, right, bottom = frame
    w, h = round(width * factor), round(height * factor)
    room_w, room_h = right-left-2*gap, bottom-top-2*gap
    if min(room_w, room_h) <= 0 or w > room_w or h > room_h:
        return None
    ref_w = 0 if edge_anchor else min(room_w, round((reference_width or width) * factor))
    ref_h = 0 if edge_anchor else min(room_h, round((reference_height or height) * factor))
    ax, ay = anchor if anchor is not None else (1, 0 if position == 'top-right' else 1)
    ax, ay = (min(1, max(0, float(value))) for value in (ax, ay))
    edge_x = left+gap+ref_w+round((room_w-ref_w)*ax)
    x = min(right-gap-w, max(left+gap, edge_x-w))
    edge_y = top+gap+ref_h+round((room_h-ref_h)*ay)
    y = min(bottom-gap-h, max(top+gap, edge_y-h))
    return x, y, w, h


def monitor_anchor_for_position(frame, geometry, x, y, dpi, *, reference_width=None, reference_height,
                                margin=16, edge_anchor=True):
    """Invert monitor placement using its bottom edge, even while minimized."""
    factor = dpi / 96
    gap = round(margin * factor)
    left, top, right, bottom = frame
    _, _, w, h = geometry
    ref_w = 0 if edge_anchor else min(right-left-2*gap, round(reference_width*factor)) if reference_width else w
    ref_h = 0 if edge_anchor else min(bottom-top-2*gap, round(reference_height * factor))
    free_x, free_y = right-left-ref_w-2*gap, bottom-top-ref_h-2*gap
    return (min(1, max(0, (x+w-left-gap-ref_w)/free_x)) if free_x > 0 else 0,
            min(1, max(0, (y+h-top-gap-ref_h)/free_y)) if free_y > 0 else 0)


def edge_anchor_from_legacy(frame, dpi, width, height, anchor, margin=16):
    """Convert stored free-space fractions once without moving the old panel."""
    factor=dpi/96;gap=round(margin*factor)
    room=(frame[2]-frame[0]-2*gap,frame[3]-frame[1]-2*gap)
    sizes=(round(width*factor),round(height*factor))
    return tuple((min(space,size)+(space-min(space,size))*value)/space if space>0 else 1
                 for space,size,value in zip(room,sizes,anchor))


def extend_monitor_left(frame, dpi, monitor, detail_width, *, margin=16):
    """Return a side extension only when the existing monitor anchor permits it."""
    x, y, w, h = monitor
    extra = round(detail_width * dpi / 96)
    if x-extra < frame[0]+round(margin*dpi/96):
        return None
    return x-extra, y, w+extra, h
