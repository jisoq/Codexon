import QtQuick
import CacheMonitor 6.0
Item {
    id: root
    required property var presentation
    readonly property var s: presentation.state
    QuickPlot { anchors.fill: parent; source: parent.presentation }
    Canvas {
        id: heartbeat
        objectName: "overlayHeartbeat"
        x: (root.s.heartbeatX || 204) * root.s.overlayScale
        y: (root.s.heartbeatY || 89) * root.s.overlayScale
        width: 48 * root.s.overlayScale; height: 18 * root.s.overlayScale
        property color ink: root.s.overlayInk || appTheme.palette.ink
        property real phase: 0
        onInkChanged: requestPaint()
        onPhaseChanged: requestPaint()
        onPaint: {
            const c = getContext("2d"); c.reset();
            c.strokeStyle = ink; c.lineWidth = 1.5 * (root.s.overlayScale || 1);
            c.lineJoin = "round"; c.lineCap = "round";
            const mid = height / 2;
            const active = !!root.s.heartbeatActive;
            const amplitude = active ? (root.s.reducedMotion ? 1 : 0.65 + 0.35 * Math.sin(phase)) : 0;
            const waveform = [[0,0],[0.25,0],[0.34,-0.18],[0.42,0.55],[0.52,-0.65],[0.62,0.2],[0.72,0],[1,0]];
            c.beginPath();
            for (let i=0;i<waveform.length;i++) {
                const x=waveform[i][0]*width, y=mid+waveform[i][1]*height*amplitude;
                if (i===0) c.moveTo(x,y); else c.lineTo(x,y);
            }
            c.stroke();
        }
        Connections { target: root; function onSChanged() { heartbeat.requestPaint(); } }
        NumberAnimation on phase {
            from: 0; to: Math.PI * 2; duration: 1000; loops: Animation.Infinite
            running: !!root.s.heartbeatActive && !!root.s.overlayVisible && !root.s.reducedMotion
        }
    }
    Text {
        x: heartbeat.x + heartbeat.width + 6 * root.s.overlayScale
        y: heartbeat.y; width: 30 * root.s.overlayScale; height: heartbeat.height
        text: root.s.heartbeatActive ? root.s.heartbeatCount : appLanguage.text("호출 대기")
        color: root.s.overlayInk || appTheme.palette.ink
        font.family: root.s.overlayFamily || "Pretendard JP"
        font.pixelSize: Math.round(10 * (root.s.overlayScale || 1))
        verticalAlignment: Text.AlignVCenter
        Accessible.role: Accessible.StaticText
        Accessible.name: root.s.heartbeatActive ? appLanguage.text("호출 진행 중") + " " + root.s.heartbeatCount : appLanguage.text("호출 대기")
    }
    Canvas {
        id: activity
        objectName: "overlay-record-loading"
        visible: !!root.s.checkingRecords
        x: root.s.loadingX * root.s.overlayScale
        y: root.s.loadingY * root.s.overlayScale
        width: 14 * root.s.overlayScale; height: width
        property color ink: root.s.overlaySecondary || root.s.overlayInk || appTheme.palette.muted
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
