import QtQuick
import QtQuick.Controls.Basic

Button {
    id: control
    property bool disclosure: false
    property bool selectionTab: false
    property string iconName: ""
    hoverEnabled: true
    implicitWidth: iconName.length ? 36 : Math.max(80, contentItem.implicitWidth + 28)
    implicitHeight: 36
    padding: 8; horizontalPadding: iconName.length ? 8 : 14
    font.family: appTheme.family; font.pixelSize: 14
    HoverHandler { cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
    contentItem: Item {
        implicitWidth: control.iconName.length ? 20 : caption.implicitWidth + (control.disclosure ? 18 : 0)
        implicitHeight: 20
        UiIcon { visible: control.iconName.length > 0; name: control.iconName; anchors.centerIn: parent; width:20;height:20; ink: control.enabled ? appTheme.palette.ink : appTheme.palette.unknown }
        Text {
            id: caption; anchors.fill:parent; visible: !control.iconName.length
            text: control.text; font: control.font; leftPadding: control.disclosure ? 18 : 0
            color: !control.enabled ? appTheme.palette.unknown : control.checked ? appTheme.palette.accent : appTheme.palette.ink
            horizontalAlignment: control.disclosure ? Text.AlignLeft : Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            Item {
                visible: control.disclosure; anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                width:10;height:10;rotation:control.checked ? 90 : 0
                Rectangle { x:1;y:2.5;width:6;height:1.5;rotation:45;color:appTheme.palette.muted }
                Rectangle { x:1;y:6.5;width:6;height:1.5;rotation:-45;color:appTheme.palette.muted }
            }
        }
    }
    background: Rectangle {
        radius: 6
        color: control.disclosure || control.flat ? "transparent" : control.checked ? appTheme.palette.secondary : appTheme.palette.surface
        border.color: control.activeFocus ? appTheme.palette.accent : control.disclosure || control.flat ? "transparent" : appTheme.palette.border
        border.width: control.activeFocus ? 2 : 1
        Rectangle { objectName:"hover-feedback";anchors.fill:parent;radius:parent.radius;color:appTheme.palette.accent;opacity:!control.enabled ? 0 : control.down ? .16 : control.hovered ? .09 : 0 }
        Rectangle { visible:control.selectionTab && control.checked;anchors.bottom:parent.bottom;anchors.left:parent.left;anchors.right:parent.right;height:2;color:appTheme.palette.accent }
        opacity: control.enabled ? 1 : .55
    }
}
