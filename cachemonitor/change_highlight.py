"""Highlight changed visible cells without formatting the entire data set."""
import time
from PySide6.QtCore import QObject, QTimer

class ChangeHighlight(QObject):
    duration=1.1
    def __init__(self,view):
        super().__init__(view);self.view=view;self.active={};view.changes=self
        self.timer=QTimer(self);self.timer.setInterval(40);self.timer.timeout.connect(self.tick)
    def key(self,row):return self.view.row_key(self.view.model().rows[row])
    def visible(self):
        view=self.view;model=view.model()
        if not view.isVisible():return {}
        return {(self.key(row),view._order[col]):str(model.data(model.index(row,col)) or '')
                for row in range(max(0,view.first_visible),min(model.rowCount(),view.last_visible+1))
                for col in range(model.columnCount())}
    def before(self):
        if not self.view.live_update:self.clear();return {}
        return self.visible()
    def after(self,previous):
        now=time.monotonic()
        for key,value in self.visible().items():
            if key in previous and previous[key]!=value:self.active[key]=now
        if self.active:self.timer.start();self.view.refresh_highlights()
    def strength(self,row,col):
        started=self.active.get((self.key(row),col))
        return max(0,1-(time.monotonic()-started)/self.duration) if started is not None else 0.
    def clear(self):self.active.clear();self.timer.stop()
    def tick(self):
        now=time.monotonic();self.active={k:v for k,v in self.active.items() if now-v<self.duration}
        self.view.refresh_highlights()
        if not self.active:self.timer.stop()
