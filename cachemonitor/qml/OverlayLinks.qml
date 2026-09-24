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
            function activate() {
                if (modelData.formula) {
                    const point = link.mapToItem(null, 0, height);
                    root.presentation.showCalculation(modelData.id, point.x, point.y);
                } else root.presentation.keyboardActivate(modelData.id);
            }
            Accessible.role: infoIcon || !!modelData.formula ? Accessible.Button : Accessible.Link
            Accessible.name: appLanguage.text(modelData.accessible)
            Accessible.onPressAction: activate()
            Keys.onReturnPressed: activate()
            Keys.onEnterPressed: activate()
            Keys.onSpacePressed: activate()
            Rectangle { anchors.fill: parent; color: "#01000000" }
            Rectangle {
                x: 0; y: parent.height - root.s.scale
                width: parent.width; height: root.s.scale; color: root.s.accent
                visible: !link.infoIcon && (pointer.containsMouse || link.activeFocus)
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
                anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                onPressed: root.presentation.captureNavigation(link.modelData.id)
                onClicked: {
                    if (link.modelData.formula) link.activate();
                    else root.presentation.activateNavigation();
                }
                onWheel: wheel => wheel.accepted = false
            }
        }
    }
}
