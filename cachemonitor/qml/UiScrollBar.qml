import QtQuick
import QtQuick.Controls.Basic

ScrollBar {
    id: control
    active: true
    padding: 2
    implicitWidth: 10; implicitHeight: 10
    opacity: size < 0.999 ? 1 : 0
    contentItem: Rectangle {
        implicitWidth: 6; implicitHeight: 6; radius: 3
        color: control.pressed ? (appTheme.palette && appTheme.color("muted")) : control.hovered ? (appTheme.palette && appTheme.color("muted")) : (appTheme.palette && appTheme.color("border"))
    }
    background: Rectangle { color: (appTheme.palette && appTheme.color("secondary")); radius: 4 }
}
