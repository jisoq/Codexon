import QtQuick
import QtQuick.Controls.Basic

Slider {
    id: control
    hoverEnabled: true
    HoverHandler { cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
    implicitWidth: 160; implicitHeight: 36
    background: Rectangle {
        x: control.leftPadding; y: (control.height-height)/2
        width: control.availableWidth; height: 4; radius: 2; color: (appTheme.palette && appTheme.color("#d8e0eb"))
        Rectangle { width: control.visualPosition*parent.width; height: parent.height; radius: 2; color: (appTheme.palette && appTheme.color("#326ba9")) }
    }
    handle: Rectangle {
        x: control.leftPadding + control.visualPosition*(control.availableWidth-width)
        y: (control.height-height)/2; width: 18; height: 18; radius: 9
        color: (appTheme.palette && appTheme.color("white")); border.color: control.enabled ? (appTheme.palette && appTheme.color("#326ba9")) : (appTheme.palette && appTheme.color("#a8b5c7"))
        border.width: control.activeFocus || control.hovered ? 3 : 2
        Rectangle { objectName:"hover-feedback";anchors.fill:parent;radius:9;color:appTheme.palette.accent;opacity:control.enabled && control.hovered ? .15 : 0 }
    }
}
