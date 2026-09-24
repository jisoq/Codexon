"""Shared dashboard palette; Codex preferences are only ever read."""
from PySide6.QtCore import QObject, Property, Signal, Slot
from PySide6.QtGui import QGuiApplication, QFontDatabase, QColor

LIGHT = dict(background='#F6F8FB', surface='#FFFFFF', secondary='#EDF2F7', ink='#182535',
             muted='#5C6C80', border='#D9E2EC', accent='#285FBC', cached='#0F766E',
             written='#8A641A', output='#6551A4', completed='#B95443', unknown='#7A8898', warning='#A45A13', error='#B42332')
DARK = dict(background='#11161D', surface='#19212B', secondary='#222D3A', ink='#EDF2F7',
            muted='#A7B5C7', border='#354253', accent='#8BB5FF', cached='#55C5B8',
            written='#D6B56B', output='#B9A4EB', completed='#F49B87', unknown='#8B99AA', warning='#E4B06A', error='#FF929D')

def contrast_color(color, background, minimum=3):
    def luminance(value):
        rgb=QColor(value).getRgbF()[:3]
        return sum(w*(v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4) for w,v in zip((.2126,.7152,.0722),rgb))
    bg=luminance(background);original=QColor(color)
    target=QColor('#ffffff' if bg<.18 else '#000000')
    for step in range(21):
        f=step/20
        candidate=QColor.fromRgbF(*(a*(1-f)+b*f for a,b in zip(original.getRgbF()[:3],target.getRgbF()[:3])))
        light=luminance(candidate)
        if (max(light,bg)+.05)/(min(light,bg)+.05)>=minimum:return candidate.name()
    return target.name()

class Theme(QObject):
    changed = Signal()
    def __init__(self, parent=None):
        super().__init__(parent); self._palette = dict(LIGHT); self._dark = False
        self._family = 'Pretendard JP'; self._reader = None

    @Slot(str, str, result=str)
    def readableText(self, foreground, background):
        return contrast_color(self.color(foreground), self._palette.get(background, self.color(background)), 4.5)

    @Property('QVariantMap', notify=changed)
    def palette(self): return self._palette
    @Property(bool, notify=changed)
    def dark(self): return self._dark
    @Property(str, notify=changed)
    def family(self): return self._family

    def configure(self, mode='codex'):
        from .overlay_appearance import CodexAppearance, default_appearance
        from .token_colors import token_palette
        from .overlay import system_dark
        dark = mode == 'dark' if mode != 'codex' else system_dark()
        palette = dict(DARK if dark else LIGHT); family = 'Pretendard JP'
        appearance = default_appearance(dark)
        if mode == 'codex':
            if self._reader is None: self._reader = CodexAppearance(families=QFontDatabase.families())
            appearance = self._reader.read(dark)
            dark = appearance.dark; palette = dict(DARK if dark else LIGHT)
        palette.update(surface=appearance.surface, ink=appearance.ink, **token_palette(appearance))
        family = appearance.family
        for key in ('warning','error','completed'):
            palette[key]=contrast_color(palette[key],palette['surface'])
        palette['muted']=contrast_color(palette['muted'],palette['surface'],4.5)
        if (palette, dark, family) != (self._palette, self._dark, self._family):
            self._palette, self._dark, self._family = palette, dark, family; self.changed.emit()

    @Slot(str, result=str)
    def color(self, value):
        """Map existing presentation tokens into the shared palette."""
        value = value.lower()
        if self._dark and value in ('#fff4e4','#ffe8c2'):return self._palette['secondary' if value=='#fff4e4' else 'border']
        for key, token in LIGHT.items():
            if token.lower() == value: return self._palette[key]
        aliases = {
            'white':'surface', '#ffffff':'surface', '#f9fafc':'background', '#f3f6fa':'secondary',
            '#fafbfd':'secondary', '#f8fafc':'secondary', '#f0f4f8':'secondary', '#f2f6fa':'secondary', '#edf0f5':'border',
            '#e1e7ef':'border', '#dfe4ea':'border', '#cdd5df':'border', '#e5eaf0':'border',
            '#263344':'ink', '#344054':'ink', '#243247':'ink', '#526278':'muted', '#66768c':'muted',
            '#63748a':'muted', '#677382':'muted', '#98a2b3':'unknown', '#94a6bd':'unknown',
            '#235b99':'accent', '#3976bb':'accent', '#326ba9':'accent', '#3466a3':'accent',
            '#18766e':'cached', '#e7f0fb':'secondary', '#edf4fc':'secondary', '#edf3fa':'secondary',
            '#f2f5f9':'secondary', '#e8edf4':'secondary', '#f2f4f7':'secondary', '#98a8bb':'muted',
            '#e7eff9':'secondary','#edf1f6':'secondary','#dfe5ee':'border','#eef2f7':'secondary',
            '#bdc8d6':'border','#94a4b8':'muted','#6f849d':'muted','#c77e23':'warning'}
        return self._palette.get(aliases.get(value), value)

def shared_theme():
    app = QGuiApplication.instance()
    if not hasattr(app, '_usage_theme'): app._usage_theme = Theme(app)
    return app._usage_theme
