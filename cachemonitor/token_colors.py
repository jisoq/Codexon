"""Bounded, accent-derived colors shared by dashboard and overlay surfaces."""
from functools import lru_cache
from math import atan2, cos, sin, radians, degrees, hypot, dist
from PySide6.QtGui import QColor

TOKEN_ROLES = ('cached', 'uncached', 'written', 'output', 'reasoning', 'completed')
TOKEN_COLORS = {False: (*TOKEN_ROLES, 'unknown'), True: (*TOKEN_ROLES, 'unknown')}

def linear_rgb(value):
    return tuple(v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4
                 for v in QColor(value).getRgbF()[:3])

@lru_cache(maxsize=16384)
def luminance(value):
    return sum(v*w for v, w in zip(linear_rgb(value), (.2126, .7152, .0722)))

def contrast_ratio(a, b):
    lo, hi = sorted((luminance(a), luminance(b)))
    return (hi+.05)/(lo+.05)

def mix(a, b, fraction):
    return QColor.fromRgbF(*(x*(1-fraction)+y*fraction for x,y in
                            zip(QColor(a).getRgbF()[:3], QColor(b).getRgbF()[:3]))).name()

@lru_cache(maxsize=16384)
def oklab(value):
    r, g, b = linear_rgb(value)
    l = (.4122214708*r + .5363325363*g + .0514459929*b)**(1/3)
    m = (.2119034982*r + .6806995451*g + .1073969566*b)**(1/3)
    s = (.0883024619*r + .2817188376*g + .6299787005*b)**(1/3)
    return (.2104542553*l + .793617785*m - .0040720468*s,
            1.9779984951*l - 2.428592205*m + .4505937099*s,
            .0259040371*l + .7827717662*m - .808675766*s)

def color_distance(a, b): return dist(oklab(a), oklab(b))

def channels(lightness, a, b):
    l=(lightness+.3963377774*a+.2158037573*b)**3
    m=(lightness-.1055613458*a-.0638541728*b)**3
    s=(lightness-.0894841775*a-1.291485548*b)**3
    linear=(4.0767416621*l-3.3077115913*m+.2309699292*s,
            -1.2684380046*l+2.6097574011*m-.3413193965*s,
            -.0041960863*l-.7034186147*m+1.707614701*s)
    return tuple(12.92*v if v<=.0031308 else 1.055*v**(1/2.4)-.055 for v in linear)

def lch(lightness, chroma, hue):
    angle=radians(hue)
    for _ in range(36):
        rgb=channels(lightness,chroma*cos(angle),chroma*sin(angle))
        if all(0<=v<=1 for v in rgb): return QColor.fromRgbF(*rgb).name()
        chroma*=.9
    return QColor.fromRgbF(*[max(0,min(1,v)) for v in channels(lightness,0,0)]).name()

@lru_cache(maxsize=4096)
def _readable(color, backgrounds, minimum):
    if all(contrast_ratio(color,bg)>=minimum for bg in backgrounds): return color
    lightness,a,b=oklab(color);chroma=hypot(a,b);hue=degrees(atan2(b,a))
    candidates=(lch(k/100,chroma,hue) for k in range(101))
    valid=[c for c in candidates if all(contrast_ratio(c,bg)>=minimum for bg in backgrounds)]
    if valid: return min(valid,key=lambda c:abs(oklab(c)[0]-lightness))
    # Mathematical gamut endpoints, not an independently assigned UI color.
    return max((lch(0,0,0),lch(1,0,0)),key=lambda c:min(contrast_ratio(c,bg) for bg in backgrounds))

def readable(color, backgrounds, minimum=4.5):
    return _readable(QColor(color).name(),tuple(QColor(bg).name() for bg in backgrounds),minimum)

@lru_cache(maxsize=128)
def categorical(accent, backgrounds, count=6, rotation=0):
    lightness,a,b=oklab(accent);chroma=hypot(a,b);hue=degrees(atan2(b,a))
    pool=[]
    for k in range(72):
        for delta in (0,-.015,.015):
            for scale in (.88,.96):
                col=lch(max(0,min(1,lightness+delta)),chroma*scale,hue+k*5)
                if all(contrast_ratio(col,bg)>=3 for bg in backgrounds): pool.append(col)
    pool=list(dict.fromkeys(pool));used=[]
    for i in range(count):
        preferred=lch(lightness,chroma*.92,hue+rotation+i*137.508)
        def score(col):
            separation=min((color_distance(col,other) for other in used),default=0)
            return separation-.38*color_distance(col,preferred)-.6*abs(oklab(col)[0]-lightness)
        used.append(max(pool,key=score) if pool else readable(preferred,backgrounds,3))
    return tuple(used)

@lru_cache(maxsize=128)
def _palette(surface, ink, accent, panel_surface):
    ink=readable(ink,(surface,),7)
    panel=mix(surface,ink,.025);overlay=panel_surface or mix(surface,ink,.055)
    backgrounds=(surface,panel,overlay)
    ink=readable(ink,backgrounds,7)
    p=dict(surface=surface,panel=panel,overlay=overlay,background=surface,
           secondary=panel,ink=ink,muted=readable(mix(surface,ink,.59),backgrounds),
           accent=accent,border=mix(surface,ink,.23),grid=mix(surface,ink,.12),
           hover=mix(surface,accent,.13),selection=mix(surface,accent,.22),
           focus=readable(accent,backgrounds,3),onaccent=readable(surface,(accent,)),
           shadow=mix(surface,ink if luminance(ink)<luminance(surface) else surface,.75))
    p.update(zip(TOKEN_ROLES,categorical(accent,backgrounds)))
    p['unknown']=p['muted'];p['track']=p['grid']
    lightness,a,b=oklab(accent);chroma=hypot(a,b);hue=degrees(atan2(b,a))
    for key,offset in (('success',125),('warning',250),('error',15)):
        p[key]=readable(lch(lightness,chroma*.9,hue+offset),backgrounds)
    for key in (*TOKEN_ROLES,'accent'):p[key+'_text']=readable(p[key],backgrounds)
    p['warning_surface']=mix(surface,p['warning'],.09)
    p['warning_hover']=mix(surface,p['warning'],.16)
    p['tooltip_surface']=panel;p['tooltip_ink']=ink
    hit=QColor(surface);hit.setAlpha(1);p['hit_surface']=hit.name(QColor.HexArgb)
    return tuple(p.items())

def ui_palette(appearance):
    return dict(_palette(appearance.surface,appearance.ink,appearance.accent,appearance.panel_surface))

def token_palette(appearance):
    p=ui_palette(appearance)
    return {key:p[key] for key in ('accent',*TOKEN_ROLES,'unknown')}

def model_palette(appearance, names):
    p=ui_palette(appearance)
    colors=categorical(appearance.accent,(p['surface'],p['panel'],p['overlay']),len(names),170)
    result={}
    for name,color in zip(names,colors):
        lightness,a,b=oklab(color)
        result[name]=mix(lch(lightness,hypot(a,b)*.78,degrees(atan2(b,a))),p['panel'],.1)
    return result
