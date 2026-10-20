import QtQuick
import CacheMonitor 6.0
Item {
    id: root
    required property var presentation
    readonly property var s: presentation.state
    QuickPlot { anchors.fill: parent; source: parent.presentation }
    Repeater {
        model: root.s.mainHeartbeats || []
        delegate: AgentHeartbeat {
            required property var modelData
            indicator: modelData
            unitScale: root.s.overlayScale || 1
            animate: !!root.s.overlayVisible && !root.s.reducedMotion
        }
    }
    Item {
        id: heartbeat
        objectName: "overlayHeartbeat"
        visible: !!root.s.totalRowVisible && (root.s.heartbeatMode === "active" || root.s.heartbeatMode === "idle")
        x: root.s.heartbeatX * root.s.overlayScale
        y: (root.s.heartbeatY || 89) * root.s.overlayScale
        width: 6 * root.s.overlayScale; height: 18 * root.s.overlayScale
        property color ink: root.s.heartbeatColor || "#b58500"
        property bool blinking: visible && !!root.s.heartbeatActive && !!root.s.overlayVisible && !root.s.reducedMotion
        opacity: 1
        Rectangle {
            anchors.centerIn: parent
            width: parent.width; height: width; radius: width / 2
            color: root.s.heartbeatMode === "active" ? heartbeat.ink : "transparent"
            border.color: heartbeat.ink
            border.width: root.s.heartbeatMode === "idle" ? 1.5 * root.s.overlayScale : 0
        }
        SequentialAnimation on opacity {
            running: heartbeat.blinking; loops: Animation.Infinite
            onStopped: heartbeat.opacity = 1
            NumberAnimation { from: 1; to: 0.35; duration: 800; easing.type: Easing.InOutSine }
            NumberAnimation { from: 0.35; to: 1; duration: 800; easing.type: Easing.InOutSine }
        }
    }
    Text {
        id: heartbeatLabel
        objectName: "overlayHeartbeatLabel"
        visible: !!root.s.totalRowVisible
        x: root.width - root.s.overlayMargin * root.s.overlayScale - width
        y: heartbeat.y; width: Math.min(implicitWidth, root.width * .45); height: heartbeat.height
        elide: Text.ElideRight
        text: root.s.heartbeatLabel || ""
        color: root.s.heartbeatMode === "error" || root.s.heartbeatMode === "pending" ? root.s.heartbeatColor : root.s.heartbeatMode === "idle" ? root.s.overlaySecondary : root.s.overlayInk || appTheme.palette.ink
        font.family: root.s.overlayFamily || "Pretendard JP"
        font.pixelSize: Math.round(11 * (root.s.overlayScale || 1))
        verticalAlignment: Text.AlignVCenter
        Accessible.role: Accessible.StaticText
        Accessible.name: text
    }
}
