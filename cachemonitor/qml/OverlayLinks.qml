import QtQuick
import QtQuick.Controls

Item {
    id: root
    required property var presentation
    property var s: presentation.state
    focus: true
    Keys.onEscapePressed: event => { root.presentation.escapePanel(); event.accepted = true; }
    Repeater {
        model: root.s.links || []
        delegate: Item {
            id: link
            required property var modelData
            objectName: "nav-" + modelData.id
            x: modelData.x * root.s.scale; y: modelData.y * root.s.scale
            width: modelData.width * root.s.scale; height: modelData.height * root.s.scale
            activeFocusOnTab: false
            property bool infoIcon: modelData.icon === "info"
            function activate() { root.presentation.activateLink(modelData.id); }
            Keys.onPressed: event => {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                    activate(); event.accepted = true;
                }
            }
            Accessible.role: modelData.interaction === "tooltip" ? Accessible.StaticText : modelData.interaction === "detail" || !!modelData.action ? Accessible.Button : Accessible.Link
            Accessible.checked: modelData.selected || false
            Accessible.name: appLanguage.text(modelData.accessible)
            Accessible.onPressAction: activate()

            Rectangle { anchors.fill: parent; color: appTheme.palette.hit_surface }
            Rectangle {
                anchors.fill: parent; radius: 5 * root.s.scale
                visible: link.modelData.interaction !== "tooltip" && !link.infoIcon
                color: root.s.accent
                opacity: pointer.pressed ? 0.16 : pointer.containsMouse ? 0.09 : 0
                border.width: link.activeFocus ? root.s.scale : 0
                border.color: root.s.accent
            }
            Rectangle {
                x: 0; y: parent.height - root.s.scale
                width: parent.width; height: root.s.scale; color: root.s.accent
                visible: modelData.interaction !== "tooltip" && !link.infoIcon && !modelData.action && !modelData.targetHint && (pointer.containsMouse || link.activeFocus)
            }
            Rectangle {
                anchors.fill: parent; radius: width / 2
                color: root.s.warning || root.s.accent
                opacity: pointer.pressed ? 0.20 : pointer.containsMouse ? 0.12 : 0
                visible: link.infoIcon
            }

            MouseArea {
                id: pointer
                anchors.fill: parent; hoverEnabled: true; cursorShape: link.modelData.interaction === "tooltip" ? Qt.ArrowCursor : Qt.PointingHandCursor
                onPressed: if (link.modelData.interaction !== "tooltip") root.presentation.captureNavigation(link.modelData.id)
                onClicked: if (link.modelData.interaction !== "tooltip") { root.forceActiveFocus(Qt.MouseFocusReason); root.presentation.activateNavigation(); }
                onWheel: wheel => wheel.accepted = false
            }
            UiToolTip {
                objectName: link.modelData.id === "tip-status" ? "heartbeatStatusTooltip" : ""
                visible: link.modelData.id === "tip-status" && pointer.containsMouse
                text: link.modelData.tooltip || ""
            }
        }
    }
}
