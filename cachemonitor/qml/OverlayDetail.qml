import QtQuick
import QtQuick.Controls
import CacheMonitor 6.0

Rectangle {
    id: root
    required property var presentation
    property var s: presentation.state
    property real unitScale: s.overlayScale || 1
    property string selectedCall: s.detailSelected || ""
    onSelectedCallChanged: if (bodyScroll) bodyScroll.contentY = 0
    color: "#01000000"
    clip: true
    Keys.onEscapePressed: event => { root.presentation.escapePanel(); event.accepted = true; }

    QuickPlot {
        id: graph
        objectName: "detailGraph"
        x: 16 * root.unitScale; y: 52 * root.unitScale
        width: (root.s.detailWidth || 208) * root.unitScale; height: 152 * root.unitScale
        source: root.presentation.detailGraph
        activeFocusOnTab: true
        property bool keyboardFocus: false
        property bool pointerFocus: false
        onActiveFocusChanged: {
            if (!activeFocus) { keyboardFocus = false; pointerFocus = false; }
            else if (!pointerFocus) keyboardFocus = true;
        }
        Accessible.role: Accessible.Chart
        Accessible.name: appLanguage.text("최근 24호출 캐시율과 API 환산 비용")
        Keys.onPressed: event => {
            pointerFocus = false; keyboardFocus = true;
            if ([Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End].indexOf(event.key) >= 0) {
                root.presentation.detailGraph.key(event.key); event.accepted = true;
            }
        }
        Rectangle {
            anchors.fill: parent; anchors.margins: -4 * root.unitScale
            visible: graph.activeFocus && graph.keyboardFocus; color: "transparent"; radius: 4 * root.unitScale
            border.width: 2 * root.unitScale; border.color: root.s.overlayAccent || "#79C4A5"
        }
        MouseArea {
            anchors.fill: parent; hoverEnabled: true
            onPositionChanged: mouse => root.presentation.detailGraph.hover_at(mouse.x, mouse.y)
            onExited: root.presentation.detailGraph.clear_hover()
            onPressed: mouse => {
                graph.pointerFocus = true;
                root.presentation.startInteraction(); graph.forceActiveFocus(Qt.MouseFocusReason);
                graph.keyboardFocus = false;
            }
            onClicked: mouse => root.presentation.detailGraph.activate_at(mouse.x, mouse.y)
        }
    }
    Flickable {
        id: bodyScroll
        objectName: "detailScroll"
        x: 16 * root.unitScale; y: 216 * root.unitScale
        width: (root.s.detailWidth || 208) * root.unitScale
        height: Math.max(0, parent.height - y - 16 * root.unitScale)
        clip: true; boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        activeFocusOnTab: true
        property bool keyboardFocus: false
        property bool pointerFocus: false
        onActiveFocusChanged: {
            if (!activeFocus) { keyboardFocus = false; pointerFocus = false; }
            else if (!pointerFocus) keyboardFocus = true;
        }
        Keys.onPressed: event => {
            pointerFocus = false; keyboardFocus = true;
            const maximum = Math.max(0, contentHeight - height);
            if ([Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End].indexOf(event.key) >= 0) {
                const amount = event.key === Qt.Key_Up ? -24 * root.unitScale : event.key === Qt.Key_Down ? 24 * root.unitScale :
                               event.key === Qt.Key_PageUp ? -height : event.key === Qt.Key_PageDown ? height : 0;
                contentY = event.key === Qt.Key_Home ? 0 : event.key === Qt.Key_End ? maximum : Math.max(0, Math.min(maximum, contentY + amount));
                event.accepted = true;
            }
        }
        contentWidth: width
        contentHeight: Math.max(height, (root.s.detailBodyHeight || 0) + (root.s.detailHasSelection ? 48 * root.unitScale : 0))
        onContentYChanged: root.presentation.detailBody.setScrollOffset(contentY)
        onMovementStarted: root.presentation.startInteraction()
        QuickPlot {
            objectName: "detailBody"
            y: bodyScroll.contentY
            width: bodyScroll.width; height: bodyScroll.height
            source: root.presentation.detailBody
            Accessible.role: Accessible.StaticText
            Accessible.name: source.state.accessible || ""
        }
        Repeater {
            model: root.s.detailLinks || []
            delegate: Item {
                id: evidenceLink
                required property var modelData
                objectName: "evidence-" + modelData.id
                x: Math.max(0, modelData.x - 4) * root.unitScale
                y: (modelData.y - 4) * root.unitScale
                width: Math.min(bodyScroll.width, modelData.width * root.unitScale)
                height: (modelData.height + 8) * root.unitScale
                activeFocusOnTab: true
                Accessible.role: Accessible.Link; Accessible.name: modelData.accessible
                Accessible.onPressAction: root.presentation.keyboardActivate(modelData.id)
                Keys.onReturnPressed: root.presentation.keyboardActivate(modelData.id)
                Rectangle {
                    x: 4 * root.unitScale; y: parent.height - 4 * root.unitScale
                    width: parent.width - 8 * root.unitScale; height: root.unitScale
                    color: root.s.overlayAccent
                    visible: evidencePointer.containsMouse || evidenceLink.activeFocus
                }
                onActiveFocusChanged: if (activeFocus) bodyScroll.contentY = Math.max(0, y - 24 * root.unitScale)
                MouseArea {
                    id: evidencePointer
                    anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onPressed: root.presentation.captureNavigation(evidenceLink.modelData.id)
                    onClicked: root.presentation.activateNavigation()
                }
            }
        }
        Button {
            id: openDashboard
            objectName: "openDashboard"
            y: (root.s.detailBodyHeight || 0) + 8 * root.unitScale
            width: bodyScroll.width; height: 36 * root.unitScale
            visible: root.s.detailHasSelection || false
            text: appLanguage.text("대시보드에서 열기")
            contentItem: Text {
                text: openDashboard.text; color: root.s.overlayInk || "#EDF2F7"
                font.family: root.s.overlayFamily || "Pretendard JP"; font.pixelSize: 14 * root.unitScale
                horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: 6 * root.unitScale; color: root.s.overlaySurface || "#222D3A"
                border.width: openDashboard.visualFocus ? 2 : 1
                border.color: openDashboard.visualFocus ? root.s.overlayAccent : root.s.overlayBorder || "#354253"
            }
            Accessible.name: appLanguage.text("선택 호출의 전체 상세를 대시보드에서 열기")
            onPressed: root.presentation.captureNavigation("selected")
            onClicked: root.presentation.activateNavigation()
            onActiveFocusChanged: if (activeFocus) bodyScroll.contentY = Math.max(0, bodyScroll.contentHeight - bodyScroll.height)
        }
        ScrollBar.vertical: ScrollBar {
            objectName: "detailScrollBar"
            parent: root
            x: bodyScroll.x + bodyScroll.width + 6 * root.unitScale
            y: bodyScroll.y; height: bodyScroll.height
            width: 4 * root.unitScale; policy: ScrollBar.AsNeeded
            onPressedChanged: if (pressed) {
                bodyScroll.pointerFocus = true;
                root.presentation.startInteraction(); bodyScroll.forceActiveFocus(Qt.MouseFocusReason);
                bodyScroll.keyboardFocus = false;
            }
            contentItem: Rectangle { radius: width / 2; color: root.s.overlayInk || "#F1F5F2"; opacity: 0.30 }
            background: Item {}
        }
    }
    Rectangle {
        x: bodyScroll.x - 4 * root.unitScale; y: bodyScroll.y - 4 * root.unitScale
        width: bodyScroll.width + 8 * root.unitScale; height: bodyScroll.height + 8 * root.unitScale
        visible: bodyScroll.activeFocus && bodyScroll.keyboardFocus
        color: "transparent"; radius: 4 * root.unitScale
        border.width: 2 * root.unitScale; border.color: root.s.overlayAccent || "#79C4A5"
    }
}
