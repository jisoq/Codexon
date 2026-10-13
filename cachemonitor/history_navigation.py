"""Small, serializable history navigation state; selection is not a route."""
from dataclasses import dataclass, field
from PySide6.QtCore import Signal, Slot
from .presentation import Group
from .table_model import LazyTable
from .ui_details import Details
from .i18n import formatted


def history_duration(value):
    if value is None:return '—'
    minutes,centiseconds=divmod(round(value*100),6000)
    seconds=f'{centiseconds/100:.2f}'
    if minutes>=60:
        hours,minutes=divmod(minutes,60)
        return formatted('{hours}시간 {minutes}분 {seconds}초',hours=hours,minutes=minutes,seconds=seconds)
    if minutes:return formatted('{minutes}분 {seconds}초',minutes=minutes,seconds=seconds)
    return formatted('{seconds}초',seconds=seconds)


class HistoryFilters(Details):
    def __init__(self,title,content):
        super().__init__(title,content,compact=True)
        self.put(kind='historyFilters',expanded=False)

    def set_expanded(self,expanded):
        super().set_expanded(expanded)
        self.put(expanded=expanded)

    @Slot()
    def dismiss(self):
        self.toggle.setChecked(False)
        self.toggle.focusRequested.emit()



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
