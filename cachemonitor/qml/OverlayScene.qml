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
    Canvas {
        id: heartbeat
        objectName: "overlayHeartbeat"
        visible: !!root.s.totalRowVisible
        x: root.s.heartbeatX * root.s.overlayScale
        y: (root.s.heartbeatY || 89) * root.s.overlayScale
        width: 18 * root.s.overlayScale; height: width
        property color ink: root.s.heartbeatColor || "#b58500"
        property bool blinking: visible && !!root.s.heartbeatActive && !!root.s.overlayVisible && !root.s.reducedMotion
        opacity: 1
        onInkChanged: requestPaint()
        onPaint: {
            const c = getContext("2d"); c.reset();
            c.strokeStyle = ink; c.lineWidth = 1.5 * (root.s.overlayScale || 1);
            c.lineJoin = "round"; c.lineCap = "round";
            const mid = height / 2;
            if (root.s.heartbeatMode === "error") {
                c.lineWidth = 2.2 * root.s.overlayScale;
                c.beginPath(); c.moveTo(width/2, height*.2); c.lineTo(width/2, height*.58); c.stroke();
                c.beginPath(); c.fillStyle=ink; c.arc(width/2,height*.8,1.2*root.s.overlayScale,0,Math.PI*2); c.fill();
                return;
            }
            if (root.s.heartbeatMode !== "active") {
                c.beginPath(); c.moveTo(2*root.s.overlayScale,mid); c.lineTo(width-2*root.s.overlayScale,mid); c.stroke();
                return;
            }
            const waveform = [[0,0],[0.25,0],[0.34,-0.18],[0.42,0.55],[0.52,-0.65],[0.62,0.2],[0.72,0],[1,0]];
            c.beginPath();
            for (let i=0;i<waveform.length;i++) {
                const x=2*root.s.overlayScale+waveform[i][0]*(width-4*root.s.overlayScale), y=mid+waveform[i][1]*height*.65;
                if (i===0) c.moveTo(x,y); else c.lineTo(x,y);
            }
            c.stroke();
        }
        Connections { target: root; function onSChanged() { heartbeat.requestPaint(); } }
        SequentialAnimation on opacity {
            running: heartbeat.blinking; loops: Animation.Infinite
            onStopped: heartbeat.opacity = 1
            NumberAnimation { from: 1; to: 0.28; duration: 450 }
            NumberAnimation { from: 0.28; to: 1; duration: 450 }
        }
    }
    Text {
        id: heartbeatLabel
        visible: heartbeat.visible
        x: root.width - 12 * root.s.overlayScale - width
        y: heartbeat.y; width: implicitWidth; height: heartbeat.height
        text: root.s.heartbeatLabel || ""
        color: root.s.overlayInk || appTheme.palette.ink
        font.family: root.s.overlayFamily || "Pretendard JP"
        font.pixelSize: Math.round(11 * (root.s.overlayScale || 1))
        verticalAlignment: Text.AlignVCenter
        Accessible.role: Accessible.StaticText
        Accessible.name: root.s.heartbeatMode === "error" ? appLanguage.text("수집 오류") : text
    }
}
