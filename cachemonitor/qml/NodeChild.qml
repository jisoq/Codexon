import QtQuick
import QtQuick.Layouts

Item {
    id: child
    property alias item: content.item
    Loader { id: content; anchors.fill: parent }
    required property var node
    property bool horizontalParent: false
    property var s: node ? node.state : ({})
    visible: !!node && s.visible
    enabled: !!node && s.enabled
    implicitWidth: item ? item.implicitWidth : 0
    implicitHeight: item ? item.implicitHeight : 0
    Layout.alignment: s.kind === "navigation" ? Qt.AlignTop : 0
    Layout.minimumWidth: s.minWidth || 0
    Layout.minimumHeight: s.minHeight || 0
    Layout.maximumWidth: s.maxWidth || Infinity
    Layout.maximumHeight: s.maxHeight || Infinity
    Layout.preferredWidth: s.width >= 0 ? s.width : implicitWidth
    Layout.preferredHeight: s.height >= 0 ? s.height : implicitHeight
    Layout.fillWidth: s.width < 0 && (!horizontalParent || s.stretch > 0 || !!s.expandX)
    Layout.fillHeight: s.height < 0 && (s.stretch > 0 || !!s.expandY || (horizontalParent && ["group","row","column","scroll","plot"].includes(s.kind)) || ["stack", "tabs", "split"].includes(s.kind))
    Component.onCompleted: content.setSource("NodeView.qml", {
        node: Qt.binding(function() { return child.node }),
        horizontalParent: Qt.binding(function() { return child.horizontalParent })
    })
}
