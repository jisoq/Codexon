"""Deterministic categorical colors, separated from the UI accent in OKLab."""
from functools import lru_cache
from math import dist

from PySide6.QtGui import QColor


TOKEN_COLORS = {
    False: dict(cached='#2D775C', uncached='#386F9E', written='#9A5875', output='#765AA3', reasoning='#8C6D30', unknown='#6C7770'),
    True: dict(cached='#79C4A5', uncached='#7CB0E5', written='#D695B0', output='#B5A0E3', reasoning='#D2B06C', unknown='#A3AEA7'),
}


def linear_rgb(value):
    return tuple(v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4
                 for v in QColor(value).getRgbF()[:3])


def contrast_ratio(a, b):
    luminances = [sum(v*w for v, w in zip(linear_rgb(c), (.2126, .7152, .0722))) for c in (a, b)]
    lo, hi = sorted(luminances)
    return (hi+.05)/(lo+.05)


@lru_cache(maxsize=4096)
def oklab(value):
    r, g, b = linear_rgb(value)
    l = (.4122214708*r + .5363325363*g + .0514459929*b)**(1/3)
    m = (.2119034982*r + .6806995451*g + .1073969566*b)**(1/3)
    s = (.0883024619*r + .2817188376*g + .6299787005*b)**(1/3)
    return (.2104542553*l + .793617785*m - .0040720468*s,
            1.9779984951*l - 2.428592205*m + .4505937099*s,
            .0259040371*l + .7827717662*m - .808675766*s)


def color_distance(a, b):
    return dist(oklab(a), oklab(b))


def readable(color, backgrounds, minimum=4.5):
    original = QColor(color).getRgbF()[:3]
    target = max(('#ffffff', '#000000'), key=lambda c: min(contrast_ratio(c, bg) for bg in backgrounds))
    channels = QColor(target).getRgbF()[:3]
    for step in range(101):
        f = step/100
        candidate = QColor.fromRgbF(*(a*(1-f)+b*f for a, b in zip(original, channels))).name()
        if all(contrast_ratio(candidate, bg) >= minimum for bg in backgrounds):
            return candidate
    return target


@lru_cache(maxsize=128)
def _token_palette(dark, accent, surface, panel_surface):
    backgrounds = (surface, panel_surface or surface)
    accent = readable(accent, backgrounds)
    defaults = {k: readable(v, backgrounds, 3) for k, v in TOKEN_COLORS[dark].items()}
    # Search only colors that remain legible on both dashboard and overlay.
    candidates = tuple(dict.fromkeys(readable(QColor.fromHsv(h, s, v).name(), backgrounds, 3)
                       for h in range(0, 360, 15) for s in (0, 75, 110, 160, 210, 255)
                       for v in (45, 90, 150, 190, 225, 255)))
    result = {'accent': accent}
    for key, preferred in defaults.items():
        used = tuple(result.values())
        remaining = tuple(v for k, v in defaults.items() if k not in result and k != key)
        if min(color_distance(preferred, c) for c in used) >= .10:
            chosen = preferred
        else:
            # Reserve other categories' colors too; repairing one collision must
            # not simply transfer it to the next token. Prefer nearby hues on ties.
            separated = [c for c in candidates
                         if min(color_distance(c, other) for other in used + remaining) >= .10]
            if not separated:
                separated = [c for c in candidates if min(color_distance(c, other) for other in used) >= .10]
            if separated:
                chosen = min(separated, key=lambda c: color_distance(c, preferred))
            else:
                chosen = max(candidates, key=lambda c: (
                    min(color_distance(c, other) for other in used)
                    - .05*color_distance(c, preferred)))
        result[key] = chosen
    return tuple(result.items())


def token_palette(appearance):
    return dict(_token_palette(appearance.dark, appearance.accent,
                              appearance.surface, appearance.panel_surface))
