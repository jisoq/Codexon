import QtQuick
import QtQuick.Controls.Basic

Button {
    id: control
    property string role: "secondary"
    readonly property bool quiet: role === "quiet" || disclosure || flat
    property bool disclosure: false
    property bool selectionTab: false
    property string iconName: ""
    property string leadingIcon: ""
    property string legendColor: ""
    property bool legendDashed: false
    readonly property bool legend: legendColor.length > 0
    readonly property bool textButton: quiet || selectionTab || legend
    readonly property color neutralInk: appTheme.palette.ink
    readonly property color actionFill: Qt.tint(appTheme.palette.surface, Qt.rgba(neutralInk.r, neutralInk.g, neutralInk.b, .09))
    readonly property color contentInk: !enabled ? appTheme.palette.unknown : legend ? appTheme.readableText(checked ? legendColor : "muted", "surface") : role === "primary" && !textButton ? appTheme.readableText("surface", "accent") : checked ? appTheme.palette.accent : appTheme.palette.ink
    hoverEnabled: true
    implicitWidth: iconName.length ? 36 : legend ? contentItem.implicitWidth + 12 : Math.max(80, contentItem.implicitWidth + 28)
    implicitHeight: legend ? 30 : 36
    padding: legend ? 5 : 8; horizontalPadding: iconName.length ? 8 : legend ? 6 : 14
    font.family: appTheme.family; font.pixelSize: 14
    font.weight: textButton && !checked ? Font.Normal : Font.DemiBold
    HoverHandler { cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
    contentItem: Item {
        implicitWidth: control.iconName.length ? 20 : caption.implicitWidth + (control.disclosure ? 18 : 0)
        implicitHeight: 20
        UiIcon { visible: control.iconName.length > 0; name: control.iconName; anchors.centerIn: parent; width:20;height:20; ink: control.contentInk }
        UiIcon { visible: !control.iconName.length && control.leadingIcon.length > 0; name: control.leadingIcon; anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; width:18;height:18; ink: control.contentInk }
        Text {
            id: caption; anchors.fill:parent; visible: !control.iconName.length
            text: control.text; font: control.font; leftPadding: control.legend ? 26 : control.disclosure ? 18 : control.leadingIcon.length ? 24 : 0
            color: control.contentInk
            horizontalAlignment: control.disclosure || control.legend ? Text.AlignLeft : Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            Item {
                visible: control.disclosure; anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                width:10;height:10;rotation:control.checked ? 90 : 0
                Rectangle { x:1;y:2.5;width:6;height:1.5;rotation:45;color:appTheme.palette.muted }
                Rectangle { x:1;y:6.5;width:6;height:1.5;rotation:-45;color:appTheme.palette.muted }
            }
            Row {
                objectName: "legendSwatch"
                visible: control.legend
                anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                spacing: control.legendDashed ? 3 : 0
                Repeater {
                    model: control.legendDashed ? 3 : 1
                    Rectangle {
                        width: control.legendDashed ? 4 : 18; height: 2
                        color: appTheme.color(control.checked ? control.legendColor : "muted")
                    }
                }
            }
        }
    }
    background: Rectangle {
        radius: control.selectionTab ? 0 : 5
        color: control.textButton ? "transparent" : control.role === "primary" ? appTheme.palette.accent : control.actionFill
        border.color: appTheme.palette.accent
        border.width: control.enabled && control.activeFocus && !control.legend ? (control.selectionTab ? 1 : 2) : 0
        Rectangle { objectName:"hover-feedback";anchors.fill:parent;radius:parent.radius;color:appTheme.palette.accent;opacity:!control.enabled || control.legend ? 0 : control.down ? .16 : control.hovered ? .09 : 0 }
        Rectangle { visible:control.legend && (control.activeFocus || control.hovered);anchors.bottom:parent.bottom;anchors.left:parent.left;anchors.right:parent.right;height:control.activeFocus ? 2 : 1;color:appTheme.palette.accent }
        Rectangle { visible:control.selectionTab && control.checked;anchors.bottom:parent.bottom;anchors.left:parent.left;anchors.right:parent.right;height:2;color:appTheme.palette.accent }
        opacity: control.enabled ? 1 : .55
    }
}
