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
        color: control.pressed ? (appTheme.palette && appTheme.color("#6f849d")) : control.hovered ? (appTheme.palette && appTheme.color("#94a4b8")) : (appTheme.palette && appTheme.color("#bdc8d6"))
    }
    background: Rectangle { color: (appTheme.palette && appTheme.color("#eef2f7")); radius: 4 }
}
