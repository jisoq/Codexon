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
    color: appTheme.palette.hit_surface
    clip: true


    Button {
        id: openDashboard
        objectName: "openDashboard"
        x: 16 * root.unitScale; y: 12 * root.unitScale
        width: implicitWidth; height: 28 * root.unitScale
        enabled: root.s.detailHasSelection || false
        text: appLanguage.text("호출 상세")
        padding: 0
        contentItem: Text {
            text: openDashboard.text
            font.family: root.s.overlayFamily || "Pretendard JP"
            font.pixelSize: 14 * root.unitScale; font.weight: Font.DemiBold
            color: openDashboard.enabled && (openDashboard.hovered || openDashboard.visualFocus) ? root.s.overlayAccent : root.s.overlayInk || appTheme.palette.ink
            verticalAlignment: Text.AlignVCenter
            font.underline: openDashboard.enabled && (openDashboard.hovered || openDashboard.visualFocus)
        }
        background: Rectangle { color: "transparent"; border.width: openDashboard.visualFocus ? 2 : 0; border.color: root.s.overlayAccent || appTheme.palette.accent }
        Accessible.name: appLanguage.text("선택 호출의 전체 상세를 대시보드에서 열기")
        onPressed: root.presentation.captureNavigation("selected")
        onClicked: root.presentation.activateNavigation()
        HoverHandler { cursorShape: openDashboard.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
    }
    ToolButton {
        id: closeDetail
        objectName: "closeDetail"
        x: ((root.s.detailWidth || 208) > 208 ? 290 : 204) * root.unitScale
        y: 12 * root.unitScale; width: 24 * root.unitScale; height: 28 * root.unitScale
        text: "×"
        contentItem: Text {
            text: closeDetail.text; color: root.s.overlayInk || appTheme.palette.ink
            font.pixelSize: 18 * root.unitScale
            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 4 * root.unitScale
            color: closeDetail.hovered ? (root.s.overlaySurface || appTheme.palette.overlay) : "transparent"
            border.width: closeDetail.visualFocus ? 2 : 0
            border.color: root.s.overlayAccent || appTheme.palette.accent
        }
        Accessible.name: appLanguage.text("상세 닫기")
        onClicked: root.presentation.escapePanel()
    }
    QuickPlot {
        id: graph
        visible: false
        objectName: "detailGraph"
        x: 16 * root.unitScale; y: 52 * root.unitScale
        width: (root.s.detailWidth || 208) * root.unitScale; height: 152 * root.unitScale
        source: root.presentation.detailGraph
        activeFocusOnTab: false

        Accessible.role: Accessible.Chart
        Accessible.name: appLanguage.text("최근 24호출 캐시율과 API 환산 비용")


        MouseArea {
            anchors.fill: parent; hoverEnabled: true
            onPositionChanged: mouse => root.presentation.detailGraph.hover_at(mouse.x, mouse.y)
            onExited: root.presentation.detailGraph.clear_hover()
            onPressed: mouse => {
                root.presentation.startInteraction(); graph.forceActiveFocus(Qt.MouseFocusReason);
            }
            onClicked: mouse => root.presentation.detailGraph.activate_at(mouse.x, mouse.y)
        }
    }
    Flickable {
        id: bodyScroll
        objectName: "detailScroll"
        x: 16 * root.unitScale; y: 52 * root.unitScale
        width: (root.s.detailWidth || 208) * root.unitScale
        height: Math.max(0, parent.height - y - 16 * root.unitScale)
        clip: true; boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        activeFocusOnTab: false


        contentWidth: width
        contentHeight: Math.max(height, (root.s.detailBodyHeight || 0))
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
        ScrollBar.vertical: ScrollBar {
            objectName: "detailScrollBar"
            parent: root
            x: bodyScroll.x + bodyScroll.width + 6 * root.unitScale
            y: bodyScroll.y; height: bodyScroll.height
            width: 4 * root.unitScale; policy: ScrollBar.AsNeeded
            onPressedChanged: if (pressed) {
                root.presentation.startInteraction(); bodyScroll.forceActiveFocus(Qt.MouseFocusReason);
            }
            contentItem: Rectangle { radius: width / 2; color: root.s.overlayInk || appTheme.palette.ink; opacity: 0.30 }
            background: Item {}
        }
    }

}
