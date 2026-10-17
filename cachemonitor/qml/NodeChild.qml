import QtQuick
import QtQuick.Layouts

Item {
    id: child
    property alias item: content.item
    property bool loadRequested: true
    property bool asynchronousLoad: false
    property bool retained: false
    readonly property bool ready: content.status === Loader.Ready
    Loader {
        id: content; anchors.fill: parent
        active: child.loadRequested || child.retained
        asynchronous: child.asynchronousLoad
        visible: status === Loader.Ready
        onLoaded: child.retained = true
    }
    Loader {
        anchors.fill: parent
        active: child.loadRequested && child.asynchronousLoad && !child.ready
        sourceComponent: Component {
            AnalysisLoading { status: ({active:true, running:true, title:"화면 준비", detail:"화면의 구성 요소를 준비하고 있습니다."}) }
        }
    }
    required property var node
    property bool horizontalParent: false
    property var s: node ? node.state : ({})
    property var buttonText: undefined
    property var labelText: undefined
    property string buttonIcon: s.iconName || ""
    property string toolTipText: s.tooltip || ""
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
        horizontalParent: Qt.binding(function() { return child.horizontalParent }),
        buttonText: Qt.binding(function() { return child.buttonText }),
        labelText: Qt.binding(function() { return child.labelText }),
        buttonIcon: Qt.binding(function() { return child.buttonIcon }),
        toolTipText: Qt.binding(function() { return child.toolTipText })
    })
}
