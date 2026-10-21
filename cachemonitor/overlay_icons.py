from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QColor
from PySide6.QtSvg import QSvgRenderer

from .token_colors import readable, ui_palette


_ASSETS = Path(__file__).parent / 'assets' / 'agent-icons'
_ICON_COUNT = 28


@dataclass(frozen=True)
class AgentMark:
    index: int | None
    color: str
    _svg: bytes = field(repr=False)

    def paint(self, painter, bounds):
        if not self._svg:
            return
        renderer = QSvgRenderer(QByteArray(self._svg))
        if not renderer.isValid():
            raise RuntimeError('Invalid bundled Codex agent icon')
        painter.save()
        try:
            renderer.render(painter, bounds)
        finally:
            painter.restore()


def agent_mark(task_id, appearance):
    if not isinstance(task_id, str) or not task_id:
        return _mark(None, appearance)
    encoded = task_id.encode('utf-16-le', 'surrogatepass')
    value = 0
    for position in range(0, len(encoded), 2):
        unit = encoded[position] + 256 * encoded[position + 1]
        value = (value * 31 + unit) % 2147483647
    return _mark(value % _ICON_COUNT, appearance)


@lru_cache(maxsize=56)
def _variant(index, dark):
    svg = (_ASSETS / f'{index:02d}-{"dark" if dark else "light"}.svg').read_bytes()
    tree = ET.fromstring(svg)
    stops = tuple(dict.fromkeys(node.get('stop-color') for node in tree.iter()
                               if node.tag.endswith('}stop')))
    colors = [QColor(stop).getRgbF()[:3] for stop in stops]
    representative = QColor.fromRgbF(*(sum(values) / len(colors) for values in zip(*colors))).name()
    return svg, representative


@lru_cache(maxsize=128)
def _mark(index, appearance):
    palette = ui_palette(appearance)
    if index is None:
        return AgentMark(None, palette['muted'], b'')
    svg, representative = _variant(index, appearance.dark)
    color = readable(representative, (palette['surface'], palette['panel'], palette['overlay']), 3)
    return AgentMark(index, color, svg)
