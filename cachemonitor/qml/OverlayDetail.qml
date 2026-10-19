import QtQuick
import QtQuick.Controls
import CacheMonitor 6.0

Rectangle {
    id: root
    required property var presentation
    property var s: presentation.state
    property real unitScale: s.overlayScale || 1
    property string sessionScope: s.detailSessionKey || ""
    property string focusAction: ""
    property int actionFocusReason: Qt.MouseFocusReason
    onSessionScopeChanged: if (bodyScroll) bodyScroll.contentY = 0
    property int focusSerial: s.detailFocusSerial || 0
    onFocusSerialChanged: Qt.callLater(() => {
        for (let i = 0; i < detailActions.count; i++) {
            const action = detailActions.itemAt(i);
            if (action && action.modelData.id === root.s.detailFocusKey) {
                bodyScroll.contentY = Math.max(0, Math.min(bodyScroll.contentHeight - bodyScroll.height, action.y));
                action.forceActiveFocus(Qt.OtherFocusReason);
                break;
            }
        }
    })
    color: appTheme.palette.hit_surface
    clip: true
    opacity: root.s.contentOpacity === undefined ? 1 : root.s.contentOpacity
    Keys.onEscapePressed: event => { root.presentation.escapePanel(); event.accepted = true; }
    Flickable {
        id: bodyScroll
        objectName: "detailScroll"
        anchors.fill: parent
        anchors.rightMargin: 8 * root.unitScale
        clip: true; boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        contentWidth: width
        contentHeight: Math.max(height, root.s.detailBodyHeight || 0)
        onContentYChanged: root.presentation.detailBody.setScrollOffset(contentY)
        onMovementStarted: root.presentation.startInteraction()
        Keys.onUpPressed: contentY = Math.max(0, contentY - 30 * root.unitScale)
        Keys.onDownPressed: contentY = Math.min(contentHeight - height, contentY + 30 * root.unitScale)
        QuickPlot {
            objectName: "detailBody"
            y: bodyScroll.contentY
            width: bodyScroll.width; height: bodyScroll.height
            source: root.presentation.detailBody
            Accessible.role: Accessible.StaticText
            Accessible.name: source.state.accessible || ""
        }
        Repeater {
            id: detailActions
            model: root.s.detailActions || []
            delegate: Button {
                id: detailAction
                required property var modelData
                objectName: "detailAction-" + modelData.id
                x: modelData.x * root.unitScale; y: modelData.y * root.unitScale
                width: Math.min(bodyScroll.width, modelData.width * root.unitScale)
                height: modelData.height * root.unitScale
                text: appLanguage.text(modelData.label)
                checkable: true; checked: modelData.expanded
                contentItem: Item {}
                background: Rectangle {
                    color: "transparent"; radius: 5 * root.unitScale
                    border.width: detailAction.visualFocus ? 2 : 0
                    border.color: root.s.overlayAccent || appTheme.palette.accent
                }
                Accessible.name: text
                Accessible.checkable: true; Accessible.checked: checked
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
            x: root.width - width; y: 0; height: root.height
            width: 4 * root.unitScale; policy: ScrollBar.AsNeeded
            onPressedChanged: if (pressed) { root.presentation.startInteraction(); bodyScroll.forceActiveFocus(Qt.MouseFocusReason); }
            contentItem: Rectangle { radius: width / 2; color: root.s.overlayInk || appTheme.palette.ink; opacity: 0.30 }
            background: Item {}
        }
    }
}
