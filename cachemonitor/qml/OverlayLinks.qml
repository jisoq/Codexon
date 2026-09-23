import QtQuick

Item {
    id: root
    required property var presentation
    property var s: presentation.state
    Repeater {
        model: root.s.links || []
        delegate: Item {
            id: link
            required property var modelData
            objectName: "nav-" + modelData.id
            x: modelData.x * root.s.scale; y: modelData.y * root.s.scale
            width: modelData.width * root.s.scale; height: modelData.height * root.s.scale
            activeFocusOnTab: true
            Accessible.role: Accessible.Link
            Accessible.name: modelData.accessible
            Accessible.onPressAction: root.presentation.keyboardActivate(modelData.id)
            Keys.onReturnPressed: root.presentation.keyboardActivate(modelData.id)
            Keys.onEnterPressed: root.presentation.keyboardActivate(modelData.id)
            Rectangle { anchors.fill: parent; color: "#01000000" }
            Rectangle {
                x: 0; y: parent.height - root.s.scale
                width: parent.width; height: root.s.scale; color: root.s.accent
                visible: pointer.containsMouse || link.activeFocus
            }
            Rectangle {
                anchors.fill: parent; visible: link.activeFocus
                color: "transparent"; border.width: 2; border.color: root.s.accent; radius: 2
            }
            MouseArea {
                id: pointer
                anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                onPressed: root.presentation.captureNavigation(link.modelData.id)
                onClicked: root.presentation.activateNavigation()
                onWheel: wheel => wheel.accepted = false
            }
        }
    }
}
