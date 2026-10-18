import QtQuick
import QtQuick.Controls
import CacheMonitor 6.0

Rectangle {
    id: root
    required property var presentation
    property var s: presentation.state
    property real unitScale: s.overlayScale || 1
    property real bodyMargin: (s.detailWidth || 208) > 208 ? 12 : 16
    property string sessionScope: (s.detailSessionKey || "") + "/" + (s.detailGroup || "sub")
    property string focusAction: ""
    property int actionFocusReason: Qt.MouseFocusReason
    onSessionScopeChanged: if (bodyScroll) bodyScroll.contentY = 0
    color: appTheme.palette.hit_surface
    clip: true


    Button {
        id: openDashboard
        objectName: "openDashboard"
        x: root.bodyMargin * root.unitScale; y: 12 * root.unitScale
        width: implicitWidth; height: 28 * root.unitScale
        enabled: root.s.detailHasSession || false
        text: appLanguage.text("Sub 상세")
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
        Accessible.name: appLanguage.text("세션 상세를 대시보드에서 열기")
        onPressed: root.presentation.captureNavigation("session")
        onClicked: root.presentation.activateNavigation()
        HoverHandler { cursorShape: openDashboard.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
    }
    Rectangle {
        id: groupingTrack
        property var segments: root.s.detailGroupSegments || []
        x: segments.length ? segments[0].x * root.unitScale : 0
        y: 16 * root.unitScale
        width: segments.length ? (segments[0].width + segments[1].width) * root.unitScale : 0
        height: 20 * root.unitScale; radius: height / 2
        color: root.s.overlayBand || appTheme.palette.panel
    }
    Repeater {
        model: root.s.detailGroupSegments || []
        delegate: Button {
            id: groupingButton
            required property var modelData
            objectName: "detailMode-" + modelData.id
            x: modelData.x * root.unitScale; y: 14 * root.unitScale
            width: modelData.width * root.unitScale; height: 24 * root.unitScale
            checked: root.s.detailGroup === modelData.id
            text: modelData.label
            padding: 0
            background: Rectangle {
                x: root.unitScale; y: 3 * root.unitScale
                width: parent.width - 2 * root.unitScale; height: 18 * root.unitScale
                radius: height / 2
                color: groupingButton.checked ? (root.s.overlaySelection || appTheme.palette.selection) : "transparent"
                border.width: groupingButton.visualFocus ? 2 : 0
                border.color: root.s.overlayAccent || appTheme.palette.accent
            }
            contentItem: Text {
                text: groupingButton.text
                color: root.s.overlayInk || appTheme.palette.ink
                font.family: root.s.overlayFamily || "Pretendard JP"
                font.pixelSize: 11 * root.unitScale
                horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
            }
            Accessible.name: appLanguage.text(modelData.id === "model" ? "모델별 정리" : "하위 에이전트별 정리")
            Accessible.checkable: true
            Accessible.checked: checked
            onPressed: { root.focusAction = ""; root.presentation.startInteraction() }
            onClicked: root.presentation.activateDetail("mode-" + modelData.id)
            HoverHandler { cursorShape: Qt.PointingHandCursor }
        }
    }
    ToolButton {
        id: closeDetail
        objectName: "closeDetail"
        x: ((root.s.detailWidth || 208) > 208 ? root.s.detailCloseX : 204) * root.unitScale
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
    Row {
        objectName: "detailCostHeader"
        x: root.bodyMargin * root.unitScale; y: 53 * root.unitScale
        spacing: 12 * root.unitScale
        Repeater {
            model: [
                { id: "main", label: "Main Agent", value: root.s.detailMainCost, size: root.s.detailMainCostSize, exact: root.s.detailMainExact, partial: root.s.detailMainPartial },
                { id: "sub", label: "Sub Agent", value: root.s.detailSubCost, size: root.s.detailSubCostSize, exact: root.s.detailSubExact, partial: root.s.detailSubPartial }
            ]
            delegate: Item {
                id: costSummary
                required property var modelData
                objectName: "detailCostSummary-" + modelData.id
                width: ((root.s.detailWidth || 208) - 12) / 2 * root.unitScale
                height: 66 * root.unitScale
                Text {
                    text: costSummary.modelData.label
                    color: root.s.overlaySecondary || appTheme.palette.muted
                    font.family: root.s.overlayFamily || "Pretendard JP"
                    font.pixelSize: 11 * root.unitScale
                }
                Text {
                    objectName: "detailCostAmount-" + costSummary.modelData.id
                    y: 19 * root.unitScale; width: parent.width
                    height: 28 * root.unitScale; verticalAlignment: Text.AlignVCenter
                    text: costSummary.modelData.value || "—"
                    color: root.s.overlayInk || appTheme.palette.ink
                    font.family: root.s.overlayFamily || "Pretendard JP"
                    font.pixelSize: (costSummary.modelData.size || 22) * root.unitScale; font.weight: Font.DemiBold
                }
                Text {
                    y: 47 * root.unitScale
                    text: costSummary.modelData.partial ? appLanguage.text("확인분") : ""
                    color: root.s.overlaySecondary || appTheme.palette.muted
                    font.family: root.s.overlayFamily || "Pretendard JP"
                    font.pixelSize: 10 * root.unitScale
                }
                HoverHandler { id: costHover }
                UiToolTip { visible: costHover.hovered; text: costSummary.modelData.exact || "" }
                Accessible.role: Accessible.StaticText
                Accessible.name: modelData.label + " " + modelData.exact + (modelData.partial ? " " + appLanguage.text("확인분") : "")
            }
        }
    }
    Rectangle {
        x: root.bodyMargin * root.unitScale; y: 120 * root.unitScale
        width: (root.s.detailWidth || 208) * root.unitScale; height: root.unitScale
        color: root.s.overlayBorder || appTheme.palette.border
    }
    Flickable {
        id: bodyScroll
        objectName: "detailScroll"
        x: root.bodyMargin * root.unitScale; y: 132 * root.unitScale
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
        Repeater {
            model: root.s.detailActions || []
            delegate: Button {
                id: detailAction
                required property var modelData
                objectName: "detailAction-" + modelData.id
                x: modelData.x * root.unitScale; y: modelData.y * root.unitScale
                width: modelData.width * root.unitScale; height: modelData.height * root.unitScale
                text: appLanguage.text(modelData.label)
                checkable: true
                checked: modelData.expanded
                contentItem: Item {}
                background: Rectangle {
                    color: "transparent"
                    radius: 5 * root.unitScale
                    border.width: detailAction.visualFocus ? 2 : 0
                    border.color: root.s.overlayAccent || appTheme.palette.accent
                }
                Accessible.name: text
                Accessible.checkable: true
                Accessible.checked: checked
                Accessible.description: appLanguage.text(checked ? "내역 접기" : "내역 펼치기")
                UiToolTip { visible: detailAction.hovered && !bodyScroll.moving; text: detailAction.modelData.tooltip || "" }
                onPressed: {
                    root.focusAction = modelData.id
                    root.actionFocusReason = detailAction.focusReason
                    root.presentation.startInteraction()
                }
                onClicked: root.presentation.activateDetail(modelData.id)
                onActiveFocusChanged: if (activeFocus) {
                    root.focusAction = modelData.id
                    root.actionFocusReason = detailAction.focusReason
                    if (y < bodyScroll.contentY) bodyScroll.contentY = y
                    else if (y + height > bodyScroll.contentY + bodyScroll.height)
                        bodyScroll.contentY = Math.min(bodyScroll.contentHeight - bodyScroll.height, y + height - bodyScroll.height)
                }
                Component.onCompleted: if (root.focusAction === modelData.id)
                    Qt.callLater(() => forceActiveFocus(root.actionFocusReason))
                HoverHandler { cursorShape: Qt.PointingHandCursor }
            }
        }
        ScrollBar.vertical: ScrollBar {
            objectName: "detailScrollBar"
            parent: root
            x: bodyScroll.x + bodyScroll.width + ((root.s.detailWidth || 208) > 208 ? 2 : 6) * root.unitScale
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
