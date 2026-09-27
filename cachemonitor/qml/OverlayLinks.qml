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
            property bool infoIcon: modelData.icon === "info"
            function activate() { root.presentation.keyboardActivate(modelData.id); }
            Accessible.role: modelData.interaction === "select" ? Accessible.RadioButton : modelData.interaction === "detail" || !!modelData.action ? Accessible.Button : Accessible.Link
            Accessible.checked: modelData.selected || false
            Accessible.name: appLanguage.text(modelData.accessible)
            onActiveFocusChanged: if (modelData.targetHint) root.presentation.inspectCall(activeFocus ? modelData.id : "")
            Accessible.onPressAction: activate()
            Keys.onReturnPressed: activate()
            Keys.onEnterPressed: activate()
            Keys.onSpacePressed: activate()
            Rectangle { anchors.fill: parent; color: appTheme.palette.hit_surface }
            Rectangle {
                x: 0; y: parent.height - root.s.scale
                width: parent.width; height: root.s.scale; color: root.s.accent
                visible: !link.infoIcon && !modelData.action && !modelData.targetHint && (pointer.containsMouse || link.activeFocus)
            }
            Rectangle {
                anchors.fill: parent; radius: width / 2
                color: root.s.warning || root.s.accent
                opacity: pointer.pressed ? 0.20 : pointer.containsMouse ? 0.12 : 0
                visible: link.infoIcon
            }
            Rectangle {
                anchors.fill: parent; visible: link.activeFocus
                color: "transparent"; border.width: 2; border.color: root.s.accent
                radius: link.infoIcon ? width / 2 : 2
            }
            MouseArea {
                id: pointer
                anchors.fill: parent; hoverEnabled: true; cursorShape: link.modelData.interaction === "select" || !!link.modelData.action ? Qt.ArrowCursor : Qt.PointingHandCursor
                onPressed: root.presentation.captureNavigation(link.modelData.id)
                onClicked: root.presentation.activateNavigation()
                onWheel: wheel => wheel.accepted = false
            }
        }
    }
}
