import QtQuick
import QtQuick.Controls

Item {
    id: root
    required property var node
    readonly property bool detailed: !!node.state.detailed
    readonly property bool showProjects: !!node.state.showProjects
    readonly property real mainX: showProjects ? 225 : 0
    readonly property real mainWidth: Math.max(0,width-mainX)
    readonly property bool splitDetail: detailed && !!node.state.keepChart && mainWidth >= 1000
    readonly property real chartWidth: splitDetail ? Math.max(560,mainWidth * 0.56) : mainWidth
    onWidthChanged: node.setAvailableWidth(width)
    Component.onCompleted: node.setAvailableWidth(width)
    NodeChild {
        node: root.node.nodes[0]
        x: 0; y: 0; width: 200; height: parent.height; visible: root.showProjects
    }
    Rectangle { visible: root.showProjects; x: 212; width: 1; height: parent.height; color: appTheme.palette.border }
    NodeChild {
        node: root.node.nodes[1]
        x: root.mainX; y: 0
        width: root.chartWidth; height: root.height - y
        visible: !root.detailed || root.splitDetail
    }
    NodeChild {
        node: root.node.nodes[2]
        x: root.mainX + (root.splitDetail ? root.chartWidth + 20 : 0); y: 0
        width: root.mainWidth - (root.splitDetail ? root.chartWidth + 20 : 0); height: root.height
        visible: root.detailed
    }
}
