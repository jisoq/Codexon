import QtQuick

Rectangle {
    id: pulse
    required property var indicator
    property real unitScale: 1
    property bool animate: false
    objectName: "agentHeartbeat-" + indicator.id
    x: indicator.x * unitScale; y: indicator.y * unitScale
    width: 4 * unitScale; height: width
    radius: width / 2
    color: indicator.color
    property bool blinking: visible && animate && indicator.mode === "active"
    opacity: indicator.mode === "active" ? 0.65 : 0.38
    SequentialAnimation on opacity {
        running: pulse.blinking; loops: Animation.Infinite
        onStopped: pulse.opacity = pulse.indicator.mode === "active" ? 0.65 : 0.38
        NumberAnimation { from: 0.65; to: 0.25; duration: 650 }
        NumberAnimation { from: 0.25; to: 0.65; duration: 650 }
    }
    Accessible.role: Accessible.StaticText
    Accessible.name: indicator.accessible
}
