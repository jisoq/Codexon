"""Presentation helpers scoped to the dashboard's usage and history pages."""
from .presentation import Text
from .i18n import tr


class RecordFields(Text):
    """Aligned, selectable fields with a plain-text representation for export."""
    kind = 'recordFields'

    def set_content(self, text, entries=(), note='', *, literal=False, text_runs=None):
        self.setText(text)
        self.put(fields=[dict(label=label, value=str(value) if literal else tr(str(value)),
                              textRuns=(text_runs or {}).get(label, []))
                         for label, value in entries if value is not None and value != ''],
                 note=note if entries else text)

    def setText(self, text):
        super().setText(text)
        self.put(fields=[], note=str(text))
