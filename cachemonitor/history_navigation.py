"""Small, serializable history navigation state; selection is not a route."""
from dataclasses import dataclass, field
from PySide6.QtCore import Signal, Slot
from .presentation import Group
from .table_model import LazyTable


class HistoryTable(LazyTable):
    expansionRequested=Signal(str)

    @Slot(int)
    def toggleRow(self,index):
        if 0<=index<self.rowCount():
            row=self.model().rows[index]
            if row.get('expandable'):
                self.selectRow(index)
                self.expansionRequested.emit(row['key'])

    @Slot(bool)
    def expandCurrent(self, expanded):
        index=self.currentRow()
        if 0<=index<self.rowCount():
            row=self.model().rows[index]
            if row.get('expandable') and row.get('expanded')!=expanded:
                self.toggleRow(index)


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
    view: str = 'sessions'
    session: tuple | None = None
    request: str | None = None
    call: str | None = None
    event: str | None = None
    project: str = ''
    expanded: set = field(default_factory=set)
    search_collapsed: set = field(default_factory=set)

    def capture(self):
        return dict(project=self.project, expanded=sorted(self.expanded),search_collapsed=sorted(self.search_collapsed))

    def restore(self, value):
        self.project=value.get('project','')
        self.expanded=set(value.get('expanded',[]))
        self.search_collapsed=set(value.get('search_collapsed',[]))

    def toggle(self, key, searching=False):
        target=self.search_collapsed if searching else self.expanded
        if key in target:target.remove(key)
        else:target.add(key)
