import QtQuick
import QtQuick.Controls

Rectangle {
    id: root
    required property var presentation
    property var s: presentation.state
    property bool moveControl: s.chromeKind === "icon" || s.chromeKind === "header"
    property real outerMargin: moveControl ? 4 * s.scale : 0
    color: moveControl ? "transparent" : appTheme.palette.hit_surface
    Keys.onEscapePressed: event => { root.presentation.escapePanel(); event.accepted = true; }

    Item {
        id: moveTarget
        objectName: root.s.chromeKind === "icon" ? "restore" : "dragTitle"
        anchors.fill: parent
        anchors.margins: root.outerMargin
        visible: root.s.chromeKind === "icon" || root.s.chromeKind === "header"
        activeFocusOnTab: visible
        property bool keyboardFocus: false
        onActiveFocusChanged: if (!activeFocus) keyboardFocus = false
        Accessible.role: root.s.chromeKind === "icon" ? Accessible.Button : Accessible.Link
        Accessible.name: root.s.chromeKind === "icon" ? appLanguage.text("복원") : (root.s.title || appLanguage.text("세션 제목")) + appLanguage.text(" · 세션 기록 열기")
        Accessible.onPressAction: if (root.s.chromeKind === "icon") root.presentation.restorePanel(); else root.presentation.openSession()
        Keys.onPressed: event => {
            keyboardFocus = true;
            const step = (event.modifiers & Qt.ShiftModifier) ? 10 : 1;
            if ([Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down].indexOf(event.key) >= 0) {
                root.presentation.movePanel(event.key === Qt.Key_Left ? -step : event.key === Qt.Key_Right ? step : 0,
                                            event.key === Qt.Key_Up ? -step : event.key === Qt.Key_Down ? step : 0);
                event.accepted = true;
            } else if (root.s.chromeKind === "icon" && [Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter].indexOf(event.key) >= 0) {
                root.presentation.restorePanel(); event.accepted = true;
            } else if (root.s.chromeKind === "header" && [Qt.Key_Return, Qt.Key_Enter].indexOf(event.key) >= 0) {
                root.presentation.openSession(); event.accepted = true;
            }
        }
    }
    Rectangle {
        anchors.fill: moveTarget
        visible: root.moveControl
        color: appTheme.palette.hit_surface
    }

    Rectangle {
        anchors.fill: parent
        anchors.margins: root.outerMargin
        radius: (root.s.chromeKind === "toolbar" ? 10 : 8) * root.s.scale
        visible: root.s.chromeKind === "toolbar" || root.s.chromeKind === "icon"
        color: root.s.surface
        opacity: root.s.opacity / 100
        border.width: root.s.scale; border.color: root.s.border
    }
    Rectangle {
        anchors.fill: parent; radius: 8 * root.s.scale
        anchors.margins: root.outerMargin
        visible: root.s.chromeKind === "icon"; color: root.s.ink
        opacity: root.s.pressed ? 0.10 : root.s.hovered ? 0.06 : 0
        Behavior on opacity { NumberAnimation { duration: root.s.reducedMotion ? 0 : 100 } }
    }
    Canvas {
        id: restoreGlyph
        anchors.centerIn: parent
        width: 14 * root.s.scale; height: width
        visible: root.s.chromeKind === "icon"
        onPaint: {
            const c = getContext("2d"); c.reset();
            c.strokeStyle = root.s.meta; c.lineWidth = 1.5;
            c.lineCap = "round"; c.lineJoin = "round";
            c.scale(root.s.scale, root.s.scale);
            c.strokeRect(1, 1, 12, 12);
            c.beginPath(); c.moveTo(1, 5); c.lineTo(13, 5); c.stroke();
        }
        Connections { target: root; function onSChanged() { restoreGlyph.requestPaint(); } }
    }
    Rectangle {
        objectName: "moveFocusRing"
        anchors.fill: moveTarget; anchors.margins: -4 * root.s.scale
        visible: moveTarget.visible && moveTarget.activeFocus && moveTarget.keyboardFocus
        radius: 12 * root.s.scale; color: "transparent"
        border.width: 2 * root.s.scale; border.color: root.s.accent
    }
    Rectangle {
        visible: root.s.chromeKind === "icon" && (root.s.speedWarning || false)
        x: 26 * root.s.scale; y: 5 * root.s.scale
        width: 5 * root.s.scale; height: width; radius: width / 2
        color: root.s.warning || root.s.accent
    }
    component IconButton: ToolButton {
        id: button
        property string glyph
        property string actionName
        property bool selected: false
        width: 24 * root.s.scale; height: width; padding: 5 * root.s.scale
        focusPolicy: Qt.TabFocus; hoverEnabled: true
        Accessible.name: actionName
        Accessible.checkable: glyph === "detail" || glyph === "opacity"
        Accessible.checked: selected
        onPressed: { root.presentation.startInteraction(); forceActiveFocus(Qt.MouseFocusReason); }
        background: Rectangle {
            radius: 6 * root.s.scale
            color: button.selected ? root.s.accent : root.s.ink
            opacity: !button.enabled ? 0 : button.down ? 0.10 : button.hovered || button.selected ? 0.06 : 0
            Behavior on opacity { NumberAnimation { duration: root.s.reducedMotion ? 0 : 100 } }
        }
        Rectangle {
            anchors.fill: parent; anchors.margins: -4 * root.s.scale
            visible: button.visualFocus; radius: 8 * root.s.scale; color: "transparent"
            border.width: 2 * root.s.scale; border.color: root.s.accent
        }
        contentItem: Canvas {
            onPaint: {
                const c = getContext("2d"); c.reset();
                c.strokeStyle = button.selected ? root.s.accent : root.s.meta; c.fillStyle = c.strokeStyle;
                c.globalAlpha = button.enabled ? 1 : 0.38;
                c.lineWidth = 1.5; c.lineCap = "round"; c.lineJoin = "round";
                c.scale(root.s.scale, root.s.scale);
                if (button.glyph === "detail") {
                    c.strokeRect(1, 1, 12, 12);
                    c.beginPath(); c.moveTo(5, 1); c.lineTo(5, 13); c.stroke();
                    if (button.selected) { c.globalAlpha = 0.35; c.fillRect(1, 1, 4, 12); }
                } else if (button.glyph === "opacity") {
                    c.beginPath(); c.arc(7, 7, 5.5, 0, Math.PI * 2); c.stroke();
                    c.beginPath(); c.arc(7, 7, 5.5, Math.PI / 2, Math.PI * 1.5); c.closePath(); c.fill();
                } else {
                    c.beginPath(); c.moveTo(2, 7); c.lineTo(12, 7); c.stroke();
                }
            }
            Connections { target: root; function onSChanged() { button.contentItem.requestPaint(); } }
            Connections { target: button; function onEnabledChanged() { button.contentItem.requestPaint(); } }
        }
    }
    Item {
        x: 4 * root.s.scale; y: 4 * root.s.scale
        width: 80 * root.s.scale; height: 24 * root.s.scale
        visible: root.s.chromeKind === "actions"
        IconButton {
            id: detailButton
            objectName: "expand"; x: 0; glyph: "detail"; selected: root.s.expanded
            actionName: root.s.expanded ? appLanguage.text("상세 닫기 · 열림") : appLanguage.text("상세 열기 · 닫힘")
            onClicked: root.presentation.expandPanel()
        }
        IconButton {
            id: opacityButton
            objectName: "opacityButton"; x: 28 * root.s.scale; glyph: "opacity"; selected: root.s.popupOpen
            actionName: root.s.popupOpen ? appLanguage.text("투명도 닫기 · 열림") : appLanguage.text("투명도 열기 · 닫힘")
            onClicked: root.presentation.toggleOpacity()
        }
        IconButton {
            objectName: "collapse"; x: 56 * root.s.scale; glyph: "minimize"; actionName: appLanguage.text("최소화")
            onClicked: root.presentation.collapsePanel()
        }
    }
    Item {
        anchors.fill: parent; visible: root.s.chromeKind === "toolbar"
        Slider {
            id: opacitySlider
            objectName: "opacity"
            x: 12 * root.s.scale; y: 8 * root.s.scale
            width: 96 * root.s.scale; height: 24 * root.s.scale
            from: 0; to: 80; stepSize: 1; value: 100 - root.s.opacity
            padding: 4 * root.s.scale; focusPolicy: Qt.StrongFocus
            Accessible.name: appLanguage.text("배경 투명도")
            Accessible.description: Math.round(value) + "%"
            onPressedChanged: if (pressed) { root.presentation.startInteraction(); forceActiveFocus(Qt.MouseFocusReason); }
            onMoved: root.presentation.transparency(Math.round(value))
            background: Rectangle {
                x: opacitySlider.leftPadding; y: opacitySlider.height / 2 - height / 2
                width: opacitySlider.availableWidth; height: 2 * root.s.scale; radius: root.s.scale
                color: root.s.meta; opacity: 0.45
            }
            handle: Rectangle {
                x: opacitySlider.leftPadding + opacitySlider.visualPosition * (opacitySlider.availableWidth - width)
                y: opacitySlider.height / 2 - height / 2
                width: 10 * root.s.scale; height: width; radius: width / 2; color: root.s.accent
                Rectangle {
                    anchors.fill: parent; anchors.margins: -4 * root.s.scale
                    visible: opacitySlider.visualFocus; radius: width / 2; color: "transparent"
                    border.width: 2 * root.s.scale; border.color: root.s.accent
                }
            }
        }
        Text {
            x: 116 * root.s.scale; y: 8 * root.s.scale
            width: 32 * root.s.scale; height: 24 * root.s.scale
            text: (100 - root.s.opacity) + "%"
            color: root.s.ink; font.family: root.s.family; font.pixelSize: 11 * root.s.scale
            font.features: { "tnum": 1 }
            horizontalAlignment: Text.AlignRight; verticalAlignment: Text.AlignVCenter
        }
    }
}
