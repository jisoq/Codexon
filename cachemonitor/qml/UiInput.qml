import QtQuick
import QtQuick.Controls.Basic

TextField {
    id: control
    implicitWidth: 220; implicitHeight: 36
    leftPadding: 12; rightPadding: 12; topPadding: 8; bottomPadding: 8
    font.family: appTheme.family; font.pixelSize: 14
    color: (appTheme.palette && appTheme.color("ink")); placeholderTextColor: (appTheme.palette && appTheme.color("muted"))
    selectionColor: (appTheme.palette && appTheme.color("selection")); selectedTextColor: (appTheme.palette && appTheme.color("ink"))
    selectByMouse: true
    background: Rectangle {
        radius: 6; color: control.enabled ? (appTheme.palette && appTheme.color("surface")) : (appTheme.palette && appTheme.color("secondary"))
        border.color: control.activeFocus ? (appTheme.palette && appTheme.color("accent")) : control.hovered ? (appTheme.palette && appTheme.color("muted")) : (appTheme.palette && appTheme.color("border"))
        border.width: control.activeFocus ? 2 : 1
    }
}
