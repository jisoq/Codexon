import QtQuick

FocusScope {
    id: root
    required property var node
    property var s: node ? node.state : ({})
    implicitHeight: 56
    activeFocusOnTab: true
    Accessible.role: Accessible.Slider
    Accessible.name: appLanguage.text("시간 탐색기")
    Accessible.description: appLanguage.text("양 끝으로 기간 조절, 가운데로 기간 이동. 방향키 이동, 더하기와 빼기 확대 축소, Home 전체, End 최신 이동.")
    property bool dragging: false
    property string dragPart: ""
    property real gestureFullStart: 0
    property real gestureFullEnd: 1
    property real originStart: 0
    property real originEnd: 1
    property real anchorTime: 0
    property real pendingStart: 0
    property real pendingEnd: 1
    readonly property real fullStart: dragging ? gestureFullStart : (s.fullStart || 0)
    readonly property real fullEnd: dragging ? gestureFullEnd : (s.fullEnd || 1)
    readonly property real start: dragging ? pendingStart : (s.start || 0)
    readonly property real end: dragging ? pendingEnd : (s.end || 1)
    readonly property real railWidth: Math.max(1, width - 32)

    function xAt(time) { return 16 + railWidth * (time - fullStart) / Math.max(1, fullEnd - fullStart) }
    function timeAt(x) { return fullStart + Math.max(0, Math.min(1, (x - 16) / railWidth)) * (fullEnd - fullStart) }
    function beginGesture(x) {
        gestureFullStart = s.fullStart; gestureFullEnd = s.fullEnd
        originStart = s.start; originEnd = s.end
        pendingStart = originStart; pendingEnd = originEnd
        anchorTime = timeAt(x)
        const leftDistance = Math.abs(x - xAt(originStart))
        const rightDistance = Math.abs(x - xAt(originEnd))
        if (Math.min(leftDistance, rightDistance) <= 14)
            dragPart = leftDistance <= rightDistance ? "start" : "end"
        else {
            dragPart = "move"
            if (x < xAt(originStart) || x > xAt(originEnd)) {
                const span = originEnd - originStart
                originStart = Math.max(gestureFullStart, Math.min(gestureFullEnd - span, anchorTime - span / 2))
                originEnd = originStart + span
            }
        }
        dragging = true; forceActiveFocus(); updateGesture(x)
    }
    function updateGesture(x) {
        if (!dragging) return
        const time = timeAt(x)
        const minimum = Math.min(1, gestureFullEnd - gestureFullStart)
        if (dragPart === "start") {
            pendingStart = Math.max(gestureFullStart, Math.min(originEnd - minimum, time))
            pendingEnd = originEnd
        } else if (dragPart === "end") {
            pendingStart = originStart
            pendingEnd = Math.min(gestureFullEnd, Math.max(originStart + minimum, time))
        } else {
            const span = originEnd - originStart
            pendingStart = Math.max(gestureFullStart, Math.min(gestureFullEnd - span, originStart + time - anchorTime))
            pendingEnd = pendingStart + span
        }
    }
    function finishGesture() {
        if (!dragging) return
        node.commit(pendingStart, pendingEnd, Math.abs(pendingEnd - gestureFullEnd) < .001); dragging = false
    }
    function cancelGesture() { dragging = false }

    Rectangle {
        x: 16; y: 18; width: root.railWidth; height: 4; radius: 2
        color: appTheme.palette.border
    }
    Rectangle {
        x: root.xAt(root.start); y: 7
        width: Math.max(1, root.xAt(root.end) - x); height: 26; radius: 4
        color: Qt.alpha(appTheme.palette.accent, .15)
        border.color: root.activeFocus ? appTheme.palette.accent : "transparent"
        border.width: 1
    }
    Repeater {
        model: [root.start, root.end]
        Rectangle {
            required property real modelData
            x: root.xAt(modelData) - 5; y: 5; width: 10; height: 30; radius: 3
            color: appTheme.palette.accent
            Rectangle { anchors.centerIn: parent; width: 2; height: 12; color: appTheme.palette.onaccent }
        }
    }
    Text {
        anchors.left: parent.left; anchors.leftMargin: 16; y: 37
        width: (parent.width - 32) / 2; text: root.s.oldestLabel || ""
        font.family: appTheme.family; font.pixelSize: 12; color: appTheme.palette.muted
        elide: Text.ElideRight
    }
    Text {
        anchors.right: parent.right; anchors.rightMargin: 16; y: 37
        width: (parent.width - 32) / 2; text: root.s.latestLabel || ""
        font.family: appTheme.family; font.pixelSize: 12; color: appTheme.palette.muted
        horizontalAlignment: Text.AlignRight; elide: Text.ElideLeft
    }
    MouseArea {
        anchors.fill: parent; hoverEnabled: true; preventStealing: true
        cursorShape: pressed || Math.min(Math.abs(mouseX-root.xAt(root.start)),Math.abs(mouseX-root.xAt(root.end))) <= 14 ? Qt.SizeHorCursor : Qt.OpenHandCursor
        onPressed: mouse => root.beginGesture(mouse.x)
        onPositionChanged: mouse => { if (pressed) root.updateGesture(mouse.x) }
        onReleased: root.finishGesture()
        onCanceled: root.cancelGesture()
    }
    Keys.onPressed: event => {
        if ([Qt.Key_Left,Qt.Key_Right,Qt.Key_Plus,Qt.Key_Equal,Qt.Key_Minus,Qt.Key_Home,Qt.Key_End].includes(event.key)) {
            node.key(event.key); event.accepted = true
        }
    }
}
