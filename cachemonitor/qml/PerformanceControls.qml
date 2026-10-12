import QtQuick

Item {
    id: root
    required property var node
    implicitHeight: 40
    readonly property bool icons: width < 430
    readonly property bool compact: width < 780
    readonly property real gap: 6
    NodeChild {
        id: period; node: root.node.nodes[0]
        width: root.icons ? 32 : root.compact ? 160 : 270
        height: 36; anchors.verticalCenter: parent.verticalCenter
        buttonIcon: root.icons ? "calendar" : ""
        toolTipText: node.textValue + (root.icons ? "\n" + root.node.nodes[3].textValue : "")
    }
    NodeChild {
        id: navigator; node: root.node.nodes[1]
        x: period.width + root.gap; width: Math.max(0,follow.x-x-root.gap)
        height: 40; anchors.verticalCenter: parent.verticalCenter
    }
    NodeChild {
        id: follow; node: root.node.nodes[2]
        x: root.width-width-(stamp.visible ? stamp.width+root.gap : 0)
        width: root.icons ? 32 : root.compact ? 94 : 132
        height: 36; anchors.verticalCenter: parent.verticalCenter
        buttonIcon: root.icons ? "right" : ""
        buttonText: root.compact ? appLanguage.text("따라가기") : undefined
    }
    NodeChild {
        id: stamp; node: root.node.nodes[3]
        visible: !root.icons; width: root.compact ? 66 : 118
        x: root.width-width; height: 36; anchors.verticalCenter: parent.verticalCenter
        labelText: root.compact ? node.textValue.slice(-8) : node.textValue
        HoverHandler { id: stampHover }
        UiToolTip { visible: stampHover.hovered; text: stamp.node.textValue }
    }
}
