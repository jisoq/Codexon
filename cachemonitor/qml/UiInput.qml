import QtQuick
import QtQuick.Controls.Basic

TextField {
    id: control
    implicitWidth: 220; implicitHeight: 36
    leftPadding: 12; rightPadding: 12; topPadding: 8; bottomPadding: 8
    font.family: appTheme.family; font.pixelSize: 14
    color: (appTheme.palette && appTheme.color("#243247")); placeholderTextColor: (appTheme.palette && appTheme.color("#77859a"))
    selectionColor: (appTheme.palette && appTheme.color("#cde1f7")); selectedTextColor: (appTheme.palette && appTheme.color("#183d68"))
    selectByMouse: true
    background: Rectangle {
        radius: 6; color: control.enabled ? (appTheme.palette && appTheme.color("white")) : (appTheme.palette && appTheme.color("#f2f4f7"))
        border.color: control.activeFocus ? (appTheme.palette && appTheme.color("#3976bb")) : control.hovered ? (appTheme.palette && appTheme.color("#98a8bb")) : (appTheme.palette && appTheme.color("#cdd5df"))
        border.width: control.activeFocus ? 2 : 1
    }
}
