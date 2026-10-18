import QtQuick
import CacheMonitor 6.0
Item {
    id: root
    required property var presentation
    readonly property var s: presentation.state
    QuickPlot { anchors.fill: parent; source: parent.presentation }
    Canvas {
        id: activity
        objectName: "overlay-record-loading"
        visible: !!root.s.checkingRecords
        x: root.s.loadingX * root.s.overlayScale
        y: root.s.loadingY * root.s.overlayScale
        width: 14 * root.s.overlayScale; height: width
        property color ink: root.s.overlaySecondary || "#808080"
        onInkChanged: requestPaint()
        onPaint: {
            const c = getContext("2d");
            c.reset();
            c.lineWidth = 2 * root.s.overlayScale;
            c.lineCap = "round";
            c.strokeStyle = ink;
            c.beginPath();
            c.arc(width/2, height/2, width/2-c.lineWidth, 0, Math.PI*1.5);
            c.stroke();
        }
        RotationAnimation on rotation {
            objectName: "overlay-record-loading-animation"
            from: 0; to: 360; duration: 1100; loops: Animation.Infinite
            running: activity.visible && !!root.s.overlayVisible
        }
    }
}
