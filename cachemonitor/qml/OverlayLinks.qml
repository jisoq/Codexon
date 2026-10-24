import QtQuick
import QtQuick.Controls

Item {
    id: root
    required property var presentation
    property var s: presentation.state
    property var links: s.links || []
    property real unitScale: s.scale || 1
    property color accent: s.accent || appTheme.palette.accent
    focus: true
    Keys.onEscapePressed: event => { root.presentation.escapePanel(); event.accepted = true; }
    Repeater {
        model: root.links
        delegate: Item {
            id: link
            required property var modelData
            objectName: "nav-" + modelData.id
            x: modelData.x * root.unitScale; y: modelData.y * root.unitScale
            width: modelData.width * root.unitScale; height: modelData.height * root.unitScale
            activeFocusOnTab: false
            property bool infoIcon: modelData.icon === "info"
            property bool passive: modelData.interaction === "tooltip" || modelData.interaction === "inspect"
            function activate() { root.presentation.activateLink(modelData.id); }
            Keys.onPressed: event => {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                    activate(); event.accepted = true;
                }
            }
            Accessible.role: passive ? Accessible.StaticText : modelData.interaction === "detail" || !!modelData.action ? Accessible.Button : Accessible.Link
            Accessible.checked: modelData.selected || false
            Accessible.name: infoIcon ? modelData.accessible : appLanguage.text(modelData.accessible)
            Accessible.onPressAction: activate()

            Rectangle { anchors.fill: parent; color: appTheme.palette.hit_surface }
            Rectangle {
                anchors.fill: parent; radius: 5 * root.unitScale
                visible: !link.passive && !link.infoIcon
                color: root.s.interactionTint || root.s.overlayInteractionTint
                opacity: pointer.pressed ? 0.16 : pointer.containsMouse ? 0.09 : 0
                border.width: link.activeFocus ? root.unitScale : 0
                border.color: root.accent
            }
            Rectangle {
                x: 0; y: parent.height - root.unitScale
                width: parent.width; height: root.unitScale; color: root.accent
                visible: !link.passive && !link.infoIcon && !modelData.action && !modelData.targetHint && (pointer.containsMouse || link.activeFocus)
            }
            Rectangle {
                anchors.fill: parent; radius: width / 2
                color: root.s.warning || root.accent
                opacity: pointer.pressed ? 0.20 : pointer.containsMouse ? 0.12 : 0
                visible: link.infoIcon
            }
            Text {
                anchors.fill: parent
                visible: link.infoIcon
                text: "ⓘ"
                color: link.modelData.color || appTheme.palette.muted
                font.family: appTheme.family
                font.pixelSize: 14 * root.unitScale
                horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
            }
            UiToolTip {
                objectName: "tooltip-" + link.modelData.id
                visible: pointer.containsMouse && (link.infoIcon || link.modelData.interaction === "tooltip")
                text: link.modelData.tooltip || ""
            }

            MouseArea {
                id: pointer
                anchors.fill: parent; hoverEnabled: true; cursorShape: link.passive ? Qt.ArrowCursor : Qt.PointingHandCursor
                onEntered: root.presentation.hoverLink(link.modelData.id, true)
                onExited: root.presentation.hoverLink(link.modelData.id, false)
                onPressed: if (!link.passive) root.presentation.captureNavigation(link.modelData.id)
                onClicked: if (!link.passive) { root.forceActiveFocus(Qt.MouseFocusReason); root.presentation.activateNavigation(); }
                onWheel: wheel => wheel.accepted = false
            }
        }
    }
}
