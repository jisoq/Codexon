import QtQuick

Rectangle {
    id: pulse
    required property var indicator
    property real unitScale: 1
    property bool animate: false
    objectName: "agentHeartbeat-" + indicator.id
    visible: indicator.mode === "active"
    x: indicator.x * unitScale; y: indicator.y * unitScale
    width: 4 * unitScale; height: width
    radius: width / 2
    color: indicator.color
    property bool blinking: visible && animate && indicator.mode === "active"
    opacity: 0.8
    SequentialAnimation on opacity {
        running: pulse.blinking; loops: Animation.Infinite
        onStopped: pulse.opacity = 0.8
        NumberAnimation { from: 0.8; to: 0.32; duration: 650 }
        NumberAnimation { from: 0.32; to: 0.8; duration: 650 }
    }
    Accessible.role: Accessible.StaticText
    Accessible.name: indicator.accessible
}
