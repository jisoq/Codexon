"""Cached, input-transparent shadow around the continuous overlay surface."""
from functools import lru_cache
import math

from PySide6.QtCore import Qt, QRect, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap, QRegion
from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsScene

from .overlay_appearance import default_appearance
from .presentation import Node
from .quick_runtime import QuickHost


def shadow_padding(dark):
    # Qt's blur bounds extend 2.5 * radius + 1. Include the downward offset.
    return math.ceil((28 if dark else 24)*2.5+1+8)


@lru_cache(maxsize=12)
def shadow_image(width, height, dark, scale, color=None):
    """Render in Qt logical pixels; the window system applies display DPI once."""
    padding=shadow_padding(dark)*scale
    panel_width,panel_height=width*scale,height*scale
    size=(math.ceil(panel_width+2*padding),math.ceil(panel_height+2*padding))
    image=QImage(*size,QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    mask=QImage(math.ceil(panel_width),math.ceil(panel_height),QImage.Format_ARGB32_Premultiplied)
    mask.fill(Qt.transparent)
    painter=QPainter(mask)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    from .token_colors import ui_palette
    shade=QColor(color or ui_palette(default_appearance(dark))['shadow'])
    shade.setAlpha(round(255*(.26 if dark else .10)))
    painter.setBrush(shade)
    painter.drawRoundedRect(QRectF(0,0,panel_width,panel_height),16*scale,16*scale)
    painter.end()

    # Native Qt performs the cached blur in C++; no image library or per-frame
    # Python convolution is needed. Only the silhouette, never the screen, blurs.
    scene=QGraphicsScene()
    item=scene.addPixmap(QPixmap.fromImage(mask))
    item.setPos(padding,padding+8*scale)
    effect=QGraphicsBlurEffect()
    effect.setBlurHints(QGraphicsBlurEffect.QualityHint)
    effect.setBlurRadius((28 if dark else 24)*scale)
    item.setGraphicsEffect(effect)
    scene.setSceneRect(QRectF(0,0,*size))
    painter=QPainter(image)
    scene.render(painter,QRectF(0,0,*size),QRectF(0,0,*size))
    # The saved surface transparency must not reveal a second black layer.
    # Clear the original panel silhouette, retaining the offset shadow outside.
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setCompositionMode(QPainter.CompositionMode_Clear)
    painter.setPen(Qt.NoPen)
    painter.setBrush(shade)
    painter.drawRoundedRect(QRectF(padding,padding,panel_width,panel_height),16*scale,16*scale)
    painter.end()
    return image


class ShadowModel(Node):
    kind='plot'

    def __init__(self):
        super().__init__()
        self.image=QImage()
        self.panel=QRectF()
        self.radius=16

    def paint(self,painter):
        if self.image.isNull():return
        # Keep the vector clip in logical coordinates when Qt renders at a
        # non-integer display scale; no cached shadow can bleed into the panel.
        outside=QPainterPath();outside.addRect(QRectF(self.image.rect()))
        panel=QPainterPath();panel.addRoundedRect(self.panel,self.radius,self.radius)
        painter.save();painter.setClipPath(outside.subtracted(panel))
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(0,0,self.image);painter.restore()


class OverlayShadow(QuickHost):
    """Controller-owned companion; never activates, shows or raises itself.

    ``set_panel_size`` accepts the unscaled 380/620 design dimensions.
    ``physical_geometry`` accepts native panel bounds and the OS display scale.
    ``clip_to_frame`` accepts native Codex (left, top, right, bottom) bounds.
    """

    def __init__(self):
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|
                         Qt.WindowDoesNotAcceptFocus|Qt.WindowTransparentForInput)
        self.setWindowTitle('Cache Monitor · Shadow')
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.appearance=default_appearance(True)
        self._panel_size=(380.,580.)
        self._render_key=None
        self._clip_key=None
        self.view=ShadowModel()
        self.set_scene(self.view,'OverlayScene.qml',transparent=True)
        self.refresh_shadow()

    @property
    def padding(self):return shadow_padding(self.appearance.dark)

    @property
    def panel_rect(self):return QRectF(self.view.panel)

    def apply_appearance(self,appearance,opacity=None):
        self.appearance=appearance
        self.refresh_shadow()

    def set_panel_size(self,width,height):
        size=(max(1.,float(width)),max(1.,float(height)))
        if size==self._panel_size:return
        self._panel_size=size
        self.refresh_shadow()

    def refresh_shadow(self):
        width,height=self._panel_size
        scale=self.appearance.scale
        from .token_colors import ui_palette
        key=(width,height,self.appearance.dark,scale,ui_palette(self.appearance)['shadow'])
        if key==self._render_key:return
        self._render_key=key
        self.view.image=shadow_image(*key)
        padding=self.padding*scale
        self.view.panel=QRectF(padding,padding,width*scale,height*scale)
        self.view.radius=16*scale
        self.resize(self.view.image.size())
        self._clip_key=None
        self.view.update()

    def physical_geometry(self,panel_geometry,device_scale):
        x,y,width,height=panel_geometry
        padding=round(self.padding*self.appearance.scale*device_scale)
        return x-padding,y-padding,width+2*padding,height+2*padding

    def clip_to_frame(self,frame,geometry,device_scale):
        key=(tuple(frame),tuple(geometry),device_scale,self.width(),self.height())
        if key==self._clip_key:return
        self._clip_key=key
        x,y,width,height=geometry
        left,top,right,bottom=frame
        x1=math.ceil(max(0,left-x)/device_scale)
        y1=math.ceil(max(0,top-y)/device_scale)
        x2=min(self.width(),math.floor(min(width,right-x)/device_scale))
        y2=min(self.height(),math.floor(min(height,bottom-y)/device_scale))
        region=QRegion(QRect(x1,y1,max(0,x2-x1),max(0,y2-y1)))
        if region==QRegion(self.rect()):
            if not self.mask().isEmpty():self.clearMask()
        elif region!=self.mask():self.setMask(region)

    def closeEvent(self,event):
        self.release_scene()
        super().closeEvent(event)
