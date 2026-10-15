"""Presentation helpers scoped to the dashboard's usage and history pages."""
from PySide6.QtCore import Signal, Slot
from .presentation import Group, Column, Text
from .i18n import tr


def labeled_control(title, control):
    """Keep a control's caption and visibility together when moving its group."""
    field = Column()
    field.setSpacing(6)
    caption = Text(title)
    caption.put(fontSize=12, color='muted')
    caption.setFixedHeight(18)
    if title: control.setAccessibleName(title)
    previous = control.parent()
    if hasattr(previous, '_nodes') and control in previous._nodes:
        previous._nodes.remove(control)
        previous.structureChanged.emit()
    field.addWidget(caption)
    field.addWidget(control)
    field.caption = caption
    control.stateChanged.connect(lambda: field.setVisible(control.isVisible()))
    field.setVisible(control.isVisible())
    return field


def move_controls(moves, *, orders=()):
    """Publish each affected group's final controls once after reparenting."""
    sources={};destinations={}
    for node,destination in moves:
        previous=node.parent()
        if previous is destination:continue
        if hasattr(previous,'_nodes') and node in previous._nodes:
            previous._nodes.remove(node);sources[previous]=None
        node.setParent(destination);node.put(stretch=0)
        destination._nodes.append(node);destinations[destination]=None
    for owner,nodes in orders:
        if owner._nodes!=nodes:
            owner._nodes=list(nodes);destinations[owner]=None
    for owner in dict.fromkeys((*sources,*destinations)):
        owner.structureChanged.emit()


class Breadcrumbs(Group):
    kind = 'breadcrumbs'
    activated = Signal(int)

    def __init__(self):
        super().__init__()
        self.entries = []
        self.put(entries=[])

    def set_entries(self, entries):
        self.entries = entries
        self.put(entries=[dict(text=item['title'], literal=item.get('literal', False),
                               current=index == len(entries)-1)
                          for index, item in enumerate(entries)])

    @Slot(int)
    def activate(self, index):
        if 0 <= index < len(self.entries)-1:
            self.activated.emit(index)


class RecordFields(Text):
    """Aligned, selectable fields with a plain-text representation for export."""
    kind = 'recordFields'

    def set_content(self, text, entries=(), note='', *, literal=False):
        self.setText(text)
        self.put(fields=[dict(label=label, value=str(value) if literal else tr(str(value)))
                         for label, value in entries if value is not None and value != ''],
                 note=note if entries else text)

    def setText(self, text):
        super().setText(text)
        self.put(fields=[], note=str(text))
