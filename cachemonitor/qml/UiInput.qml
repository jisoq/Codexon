import QtQuick
import QtQuick.Controls.Basic

TextField {
    id: control
    property bool searchIcon: false
    property bool clearButton: false
    property string clearText: ""
    signal clearRequested()
    implicitWidth: 220; implicitHeight: 36
    leftPadding: searchIcon ? 36 : 12; rightPadding: clear.visible ? 40 : 12; topPadding: 8; bottomPadding: 8
    font.family: appTheme.family; font.pixelSize: 14
    color: control.enabled ? appTheme.palette.ink : appTheme.palette.unknown; placeholderTextColor: (appTheme.palette && appTheme.color("muted"))
    selectionColor: (appTheme.palette && appTheme.color("selection")); selectedTextColor: (appTheme.palette && appTheme.color("ink"))
    selectByMouse: true
    HoverHandler { cursorShape: control.enabled ? Qt.IBeamCursor : Qt.ArrowCursor }
    UiIcon { visible: control.searchIcon; name: "search"; x: 11; anchors.verticalCenter: parent.verticalCenter; width: 17; height: 17; ink: control.enabled ? appTheme.palette.muted : appTheme.palette.unknown }
    UiButton {
        id: clear; objectName: "inputClear"
        visible: control.clearButton && control.text.length > 0 && !control.readOnly
        enabled: control.enabled
        activeFocusOnTab: visible && enabled
        anchors.right: parent.right; anchors.rightMargin: 4; anchors.verticalCenter: parent.verticalCenter
        width: 28; height: 28; iconName: "close"; role: "quiet"
        Accessible.name: control.clearText
        UiToolTip { visible: clear.hovered; text: control.clearText }
        onClicked: { control.clearRequested(); control.forceActiveFocus(Qt.MouseFocusReason) }
    }
    background: Rectangle {
        radius: 3; color: control.enabled ? (appTheme.palette && appTheme.color("surface")) : (appTheme.palette && appTheme.color("secondary"))
        border.color: control.activeFocus ? (appTheme.palette && appTheme.color("accent")) : control.hovered ? (appTheme.palette && appTheme.color("muted")) : (appTheme.palette && appTheme.color("border"))
        border.width: control.activeFocus ? 2 : 1
    }
}
