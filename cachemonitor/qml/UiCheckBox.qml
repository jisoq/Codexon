import QtQuick
import QtQuick.Controls.Basic

CheckBox {
    id: control
    hoverEnabled: true
    HoverHandler { cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
    implicitHeight: 36
    spacing: 9; padding: 0
    font.family: appTheme.family; font.pixelSize: 14
    background: Rectangle { objectName:"hover-feedback";color:appTheme.palette.accent;radius:6;opacity:control.enabled && control.hovered ? .09 : 0 }
    indicator: Rectangle {
        x: control.leftPadding; anchors.verticalCenter: parent.verticalCenter
        implicitWidth: 18; implicitHeight: 18; radius: 4
        color: control.checked ? (appTheme.palette && appTheme.color("accent")) : (appTheme.palette && appTheme.color("surface"))
        border.color: control.activeFocus ? (appTheme.palette && appTheme.color("focus")) : control.checked ? (appTheme.palette && appTheme.color("accent")) : (appTheme.palette && appTheme.color("muted"))
        border.width: control.activeFocus ? 2 : 1
        Text { anchors.centerIn: parent; text: "✓"; visible: control.checked; color: appTheme.palette.onaccent; font.pixelSize: 14 }
    }
    contentItem: Text {
        text: control.text; font: control.font; color: control.enabled ? (appTheme.palette && appTheme.color("ink")) : (appTheme.palette && appTheme.color("unknown"))
        leftPadding: control.indicator.width + control.spacing
        verticalAlignment: Text.AlignVCenter
    }
}
