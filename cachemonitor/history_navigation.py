"""Small, serializable history navigation state; selection is not a route."""
from dataclasses import dataclass, field
from PySide6.QtCore import Signal, Slot
from .presentation import Group
from .table_model import LazyTable


class HistoryTree(LazyTable):
    @Slot(bool)
    def expandCurrent(self, expanded):
        index=self.currentRow()
        if 0<=index<self.rowCount():
            row=self.model().rows[index]
            if row.get('expandable') and row.get('expanded')!=expanded:
                self.cellClicked.emit(index,0)


class HistoryWorkspace(Group):
    kind='historyWorkspace'
    resized=Signal()
    available_width=0

    @Slot(float)
    def setAvailableWidth(self, width):
        if self.available_width!=width:
            self.available_width=width
            self.resized.emit()


@dataclass
class HistoryNavigation:
    view: str = 'projects'
    session: tuple | None = None
    request: str | None = None
    call: str | None = None
    event: str | None = None
    project: str = ''
    expanded: set = field(default_factory=set)

    def capture(self):
        return dict(project=self.project, expanded=sorted(self.expanded))

    def restore(self, value):
        self.project=value.get('project','')
        self.expanded=set(value.get('expanded',[]))

    def toggle(self, key):
        if key in self.expanded:self.expanded.remove(key)
        else:self.expanded.add(key)
