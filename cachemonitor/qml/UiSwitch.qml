import QtQuick
import QtQuick.Controls.Basic

Switch {
    id: control
    hoverEnabled: true
    HoverHandler { cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
    implicitWidth: 44; implicitHeight: 36; padding: 0
    background: Rectangle { objectName:"hover-feedback";color:appTheme.palette.accent;radius:6;opacity:control.enabled && control.hovered ? .09 : 0 }
    indicator: Rectangle {
        anchors.verticalCenter: parent.verticalCenter
        implicitWidth: 42; implicitHeight: 24; radius: 12
        color: !control.enabled ? (appTheme.palette && appTheme.color("#d8dee7")) : control.checked ? (appTheme.palette && appTheme.color("#326ba9")) : (appTheme.palette && appTheme.color("#98a4b5"))
        border.width: control.activeFocus ? 2 : 0; border.color: (appTheme.palette && appTheme.color("#194d86"))
        Rectangle {
            x: control.checked ? 21 : 3; y: 3; width: 18; height: 18; radius: 9
            color: (appTheme.palette && appTheme.color("white"))
        }
    }
}
