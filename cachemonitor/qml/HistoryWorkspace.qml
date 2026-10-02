import QtQuick
import QtQuick.Controls

Item {
    id: root
    required property var node
    readonly property bool narrow: width < 1100
    readonly property bool wide: width >= 1800
    readonly property bool detailed: !!node.state.detailed
    property bool navigationOpen: false
    property string routeToken: node.state.routeToken || ""
    onRouteTokenChanged: navigationOpen = false
    readonly property real navigationWidth: narrow ? 0 : 248
    readonly property real sideWidth: wide ? 400 : 0
    readonly property real mainX: navigationWidth ? navigationWidth + 16 : 0
    readonly property real mainWidth: width - mainX - (sideWidth ? sideWidth + 16 : 0)
    onWidthChanged: node.setAvailableWidth(width)
    Component.onCompleted: node.setAvailableWidth(width)
    UiButton {
        id: openNavigation
        visible: root.narrow && !root.detailed
        text: appLanguage.text("프로젝트 · 세션")
        onClicked: root.navigationOpen = !root.navigationOpen
    }
    NodeChild {
        node: root.node.nodes[0]
        x: 0; y: root.narrow ? 42 : 0
        width: 248; height: parent.height - y
        visible: !root.narrow || root.navigationOpen
        z: 5
        Rectangle { anchors.fill: parent; color: appTheme.palette.surface; z: -1 }
    }
    NodeChild {
        node: root.node.nodes[2]
        x: root.wide ? root.width - 400 : root.mainX
        y: root.narrow ? 42 : 0
        width: root.wide ? 400 : root.mainWidth
        height: root.wide ? root.height : 72
        visible: !root.detailed
    }
    NodeChild {
        node: root.node.nodes[1]
        x: root.mainX; y: root.detailed || root.wide ? 0 : (root.narrow ? 122 : 80)
        width: root.mainWidth; height: root.height - y
        visible: !root.detailed || root.wide
    }
    NodeChild {
        node: root.node.nodes[3]
        x: root.wide ? root.width - 400 : root.mainX; y: 0
        width: root.wide ? 400 : root.mainWidth; height: root.height
        visible: root.detailed
    }
}
