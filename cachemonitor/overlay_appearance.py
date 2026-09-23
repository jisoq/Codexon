"""Read Codex desktop appearance preferences without modifying its config."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tomllib
import math


@dataclass(frozen=True)
class Appearance:
    dark: bool = True
    surface: str = '#1C211E'
    ink: str = '#F1F5F2'
    accent: str = '#80cbc4'
    warning: str = '#EDB563'
    contrast: float = 60
    family: str = 'Pretendard JP'
    font_size: float = 14
    panel_surface: str | None = None

    @property
    def scale(self):
        return max(1., self.font_size / 14)


def default_appearance(dark):
    return Appearance() if dark else Appearance(False, '#F7F8F7', '#1D2521', '#386F9E', '#9B5E17')


def color(value, fallback):
    return value if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value) else fallback


def number(value, fallback, low, high):
    return min(high, max(low, value)) if type(value) in (int, float) and math.isfinite(value) else fallback


def raised_surface(surface, dark):
    channels = [int(surface[i:i+2], 16) for i in (1, 3, 5)]
    # Preserve the chrome hue while giving floating panels their own surface.
    # Pure-white themes need a small shade instead of an invisible white tint.
    target, mix = (255, .035 if dark else .10)
    if not dark and min(channels) >= 250: target, mix = 0, .025
    return '#' + ''.join(f'{round(channel*(1-mix)+target*mix):02x}' for channel in channels)


def resolve_appearance(desktop, system_dark, families=()):
    mode = desktop.get('appearanceTheme', 'system')
    dark = system_dark if mode not in ('light', 'dark') else mode == 'dark'
    # Explicit chrome values are authoritative. Missing fields retain readable
    # overlay defaults; code syntax themes do not style the app chrome.
    fallback = default_appearance(dark)
    theme = desktop.get('appearanceDarkChromeTheme' if dark else 'appearanceLightChromeTheme', {})
    if not isinstance(theme, dict): theme = {}
    fonts = theme.get('fonts', {})
    family = fonts.get('ui') if isinstance(fonts, dict) else None
    available = {s.casefold(): s for s in families}
    selected = 'Pretendard JP'
    if isinstance(family, str):
        for candidate in family.split(','):
            candidate = candidate.strip().strip('\"\'')
            if candidate.casefold() in available:
                selected = available[candidate.casefold()]; break
    semantic = theme.get('semanticColors', {})
    if not isinstance(semantic, dict): semantic = {}
    surface = color(theme.get('surface'), fallback.surface)
    return Appearance(dark, surface,
                      color(theme.get('ink'), fallback.ink), color(theme.get('accent'), fallback.accent),
                      fallback.warning,
                      number(theme.get('contrast'), 60 if dark else 45, 0, 100), selected,
                      number(desktop.get('sansFontSize'), 14, 1, float('inf')),
                      raised_surface(surface, dark) if surface != fallback.surface else surface)


class CodexAppearance:
    def __init__(self, path=None, families=()):
        home = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))
        self.path = Path(path) if path is not None else home/'config.toml'
        self.families = families
        self.signature = None
        self.desktop = {}
        self.issue = ''

    def read(self, system_dark):
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature != self.signature:
                if stat.st_size > 2 * 1024 * 1024: raise ValueError('configuration too large')
                parsed = tomllib.loads(self.path.read_text(encoding='utf-8-sig'))
                desktop = parsed.get('desktop', {})
                if not isinstance(desktop, dict): raise ValueError('invalid desktop settings')
                # Keep only appearance data, never unrelated config or credentials.
                self.desktop = {k: desktop[k] for k in ('appearanceTheme', 'appearanceLightChromeTheme',
                    'appearanceDarkChromeTheme', 'sansFontSize') if k in desktop}
                self.signature = signature
            self.issue = ''
        except FileNotFoundError:
            self.desktop = {}; self.signature = None; self.issue = 'Codex 모양 설정 없음'
        except (OSError, ValueError):
            # An atomic replacement or partial write must not flash an unrelated theme.
            self.issue = 'Codex 모양 설정 읽기 지연'
        return resolve_appearance(self.desktop, system_dark, self.families)
