"""Role-based shared palette; Codex preferences are only ever read."""
from PySide6.QtCore import QObject, Property, Signal, Slot, Qt
from PySide6.QtGui import QGuiApplication, QFontDatabase, QColor
from .token_colors import ui_palette, readable, model_palette, color_distance
from .overlay_appearance import CodexAppearance, default_appearance

def contrast_color(color, background, minimum=3):
    return readable(color,(background,),minimum)

class Theme(QObject):
    changed = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.appearance=default_appearance(False)
        self._palette=ui_palette(self.appearance);self._dark=False
        self._family='Pretendard JP';self._reader=None;self._models=[];self._model_colors={}

    @Slot(str, str, result=str)
    def readableText(self, foreground, background):
        return readable(self.color(foreground),(self.color(background),))

    @Property('QVariantMap', notify=changed)
    def palette(self): return self._palette
    @Property(bool, notify=changed)
    def dark(self): return self._dark
    @Property(str, notify=changed)
    def family(self): return self._family

    def configure(self, mode='codex', appearance=None):
        from .overlay import system_dark
        if appearance is None:
            dark=mode=='dark' if mode not in ('codex','system') else system_dark()
            if mode=='codex':
                if self._reader is None:self._reader=CodexAppearance(families=QFontDatabase.families())
                appearance=self._reader.read(dark)
            else:appearance=default_appearance(dark)
        palette=ui_palette(appearance)
        if (palette,appearance.dark,appearance.family)!=(self._palette,self._dark,self._family):
            self.appearance=appearance;self._palette=palette;self._dark=appearance.dark;self._family=appearance.family
            self._model_colors=model_palette(appearance,self._models)
            self.changed.emit()

    def register_models(self,names):
        # Append identities once; filters, amounts, reasoning and themes never reorder them.
        new=sorted(set(names)-set(self._models))
        if new:
            self._models.extend(new)
            self._model_colors=model_palette(self.appearance,self._models)

    def model_color(self,name):
        self.register_models([name])
        return self._model_colors[name]

    def model_pattern(self,name):
        color=self.model_color(name)
        if any(other!=name and color_distance(color,value)<.04 for other,value in self._model_colors.items()):
            return (Qt.BDiagPattern,Qt.FDiagPattern,Qt.HorPattern,Qt.VerPattern,Qt.CrossPattern,Qt.DiagCrossPattern)[self._models.index(name)%6]
        return None

    @Slot(str, result=str)
    def color(self, value):
        if value.startswith('model:'):return self.model_color(value[6:])
        if value in self._palette:return self._palette[value]
        if value=='transparent':return value
        return value if QColor(value).isValid() else self._palette['ink']

def shared_theme():
    app=QGuiApplication.instance()
    if not hasattr(app,'_usage_theme'):app._usage_theme=Theme(app)
    return app._usage_theme
