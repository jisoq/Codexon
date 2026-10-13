import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var node
    implicitWidth: 0; implicitHeight: 0
    Popup {
        id: popup
        objectName: "history-filter-popup"
        parent: Overlay.overlay
        modal: true; focus: true
        width: Math.min(800,parent.width-40)
        height: Math.min(body.implicitHeight+92,parent.height-48)
        x: (parent.width-width)/2; y: (parent.height-height)/2
        padding: 20
        visible: !!root.node.state.expanded
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onClosed: root.node.dismiss()
        background: Rectangle { color: appTheme.palette.surface; border.color: appTheme.palette.border; radius: 8 }
        Overlay.modal: Rectangle { color: "#40000000" }
        contentItem: ColumnLayout {
            spacing: 16
            RowLayout {
                Layout.fillWidth: true
                Text { text: appLanguage.text("조회 조건");font.family: appTheme.family;font.pixelSize: 18;font.bold: true;color: appTheme.palette.ink;Layout.fillWidth: true }
                UiButton { text: appLanguage.text("닫기");onClicked: popup.close();Accessible.name: appLanguage.text("조회 조건 닫기") }
            }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                NodeChild { id: body;node: root.node.nodes[0];width: parent.width;height: implicitHeight }
            }
        }
    }
}
