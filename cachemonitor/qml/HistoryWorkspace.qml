import QtQuick
import QtQuick.Controls

Item {
    id: root
    required property var node
    readonly property bool detailed: !!node.state.detailed
    readonly property real mainX: 225
    readonly property real mainWidth: Math.max(0,width-mainX)
    onWidthChanged: node.setAvailableWidth(width)
    Component.onCompleted: node.setAvailableWidth(width)
    NodeChild {
        node: root.node.nodes[0]
        x: 0; y: 0; width: 200; height: parent.height
    }
    Rectangle { x: 212; width: 1; height: parent.height; color: appTheme.palette.border }
    NodeChild {
        node: root.node.nodes[1]
        x: root.mainX; y: 0
        width: root.mainWidth; height: root.height - y
        visible: !root.detailed
    }
    NodeChild {
        node: root.node.nodes[2]
        x: root.mainX; y: 0
        width: root.mainWidth; height: root.height
        visible: root.detailed
    }
}
