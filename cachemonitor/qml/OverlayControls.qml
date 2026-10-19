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
        activeFocusOnTab: false
        HoverHandler { id: moveHover }
        UiToolTip { visible: moveHover.hovered; text: appLanguage.text(root.s.chromeKind === "icon" ? "오버레이 펼치기" : "끌어서 이동") }

        Accessible.role: root.s.chromeKind === "icon" ? Accessible.Button : Accessible.Pane
        Accessible.name: root.s.chromeKind === "icon" ? appLanguage.text("복원") : (root.s.title || appLanguage.text("세션 제목")) + appLanguage.text(", 이동")
        Accessible.onPressAction: if (root.s.chromeKind === "icon") root.presentation.restorePanel()

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
        opacity: root.s.chromeKind === "toolbar" ? 1 : root.s.opacity / 100
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
        width: 22 * root.s.scale; height: width; padding: 5 * root.s.scale
        focusPolicy: Qt.NoFocus; hoverEnabled: true
        Accessible.name: actionName
        UiToolTip { visible: button.hovered && !(button.glyph === "opacity" && root.s.popupOpen); text: button.actionName }
        Accessible.checkable: glyph === "settings" || glyph === "opacity"
        Accessible.checked: selected
        onPressed: { root.presentation.startInteraction(); forceActiveFocus(Qt.MouseFocusReason); }
        background: Rectangle {
            radius: 6 * root.s.scale
            color: button.selected ? root.s.accent : root.s.ink
            opacity: !button.enabled ? 0 : button.down ? 0.18 : button.hovered ? (button.selected ? 0.14 : 0.10) : button.selected ? 0.06 : 0
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
                c.scale(root.s.scale * 12 / 14, root.s.scale * 12 / 14);
                if (button.glyph === "settings") {
                    c.beginPath();
                    for (let i = 0; i < 24; i++) {
                        const angle = i * Math.PI / 12;
                        const radius = i % 3 === 0 ? 6 : 4.8;
                        const x = 7 + Math.cos(angle) * radius, y = 7 + Math.sin(angle) * radius;
                        if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
                    }
                    c.closePath(); c.stroke();
                    c.beginPath(); c.arc(7, 7, 2, 0, Math.PI * 2); c.stroke();
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
        width: 70 * root.s.scale; height: 22 * root.s.scale
        visible: root.s.chromeKind === "actions"
        IconButton {
            objectName: "settingsButton"; x: 0; glyph: "settings"; selected: root.s.popupOpen && root.s.popupKind === "settings"
            actionName: appLanguage.text("오버레이 설정")
            onClicked: root.presentation.toggleSettings()
        }
        IconButton {
            id: opacityButton
            objectName: "opacityButton"; x: 24 * root.s.scale; glyph: "opacity"; selected: root.s.popupOpen && root.s.popupKind !== "settings"
            actionName: root.s.popupOpen ? appLanguage.text("배경 투명도 조절") : appLanguage.text("배경 투명도 조절")
            onClicked: root.presentation.toggleOpacity()
        }
        IconButton {
            objectName: "collapse"; x: 48 * root.s.scale; glyph: "minimize"; actionName: appLanguage.text("최소화")
            onClicked: root.presentation.collapsePanel()
        }
    }
    Item {
        anchors.fill: parent; visible: root.s.chromeKind === "toolbar" && root.s.popupKind !== "settings"
        Slider {
            id: opacitySlider
            objectName: "opacity"
            x: 12 * root.s.scale; y: 8 * root.s.scale
            width: 96 * root.s.scale; height: 24 * root.s.scale
            from: 0; to: 80; stepSize: 1; value: 100 - root.s.opacity
            padding: 4 * root.s.scale; focusPolicy: Qt.NoFocus
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
    Item {
        anchors.fill: parent
        visible: root.s.chromeKind === "toolbar" && root.s.popupKind === "settings"
        Text {
            x: 12 * root.s.scale; y: 10 * root.s.scale
            text: appLanguage.text("차트 표시 방식")
            color: root.s.ink; font.family: root.s.family; font.pixelSize: 11 * root.s.scale
        }
        Row {
            x: 12 * root.s.scale; y: 31 * root.s.scale
            spacing: 8 * root.s.scale
            Repeater {
                model: [{ key: "bar", label: "막대" }, { key: "filled-line", label: "채움 선형" }]
                delegate: Button {
                    id: styleButton
                    required property var modelData
                    objectName: "chartStyle-" + modelData.key
                    width: 114 * root.s.scale; height: 28 * root.s.scale
                    checkable: true; checked: root.s.chartStyle === modelData.key
                    text: appLanguage.text(modelData.label)
                    onPressed: root.presentation.startInteraction()
                    onClicked: root.presentation.setChartStyle(modelData.key)
                    Accessible.name: text; Accessible.checked: checked
                    background: Rectangle {
                        radius: 5 * root.s.scale
                        color: styleButton.checked ? root.s.accent : root.s.ink
                        opacity: styleButton.checked ? 0.2 : styleButton.hovered ? 0.12 : 0.06
                        border.width: styleButton.visualFocus ? 2 : 0; border.color: root.s.accent
                    }
                    contentItem: Text {
                        text: styleButton.text; color: root.s.ink
                        font.family: root.s.family; font.pixelSize: 11 * root.s.scale
                        verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter
                    }
                }
            }
        }
        Text {
            x: 12 * root.s.scale; y: 76 * root.s.scale
            text: appLanguage.text("오버레이 높이")
            color: root.s.ink; font.family: root.s.family; font.pixelSize: 11 * root.s.scale
        }
        Row {
            x: 12 * root.s.scale; y: 98 * root.s.scale; spacing: 8 * root.s.scale
            Button {
                id: shorter
                objectName: "shorter"
                width: 32 * root.s.scale; height: 28 * root.s.scale
                enabled: root.s.canShorten || false; text: "−"
                Accessible.name: appLanguage.text("높이 줄이기")
                onPressed: root.presentation.startInteraction()
                onClicked: root.presentation.changeHeight(-80)
                background: Rectangle { radius: 5 * root.s.scale; color: root.s.ink; opacity: shorter.enabled ? (shorter.hovered ? 0.12 : 0.06) : 0.025; border.width: shorter.visualFocus ? 2 : 0; border.color: root.s.accent }
                contentItem: Text { text: shorter.text; color: root.s.ink; opacity: shorter.enabled ? 1 : 0.35; font.pixelSize: 16 * root.s.scale; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            }
            Text {
                width: 156 * root.s.scale; height: 28 * root.s.scale
                text: root.s.displayedHeight === root.s.heightValue ? root.s.heightValue + " px" : root.s.displayedHeight + " / " + root.s.heightValue + " px"
                color: root.s.ink; font.family: root.s.family; font.pixelSize: 11 * root.s.scale
                verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter
                HoverHandler { id: heightHover }
                UiToolTip { visible: heightHover.hovered; text: appLanguage.text(root.s.canAdjustHeight ? "표시 높이 / 설정 높이" : "하위 에이전트가 있을 때 높이를 조절합니다.") }
            }
            Button {
                id: taller
                objectName: "taller"
                width: 32 * root.s.scale; height: 28 * root.s.scale
                enabled: root.s.canLengthen || false; text: "+"
                Accessible.name: appLanguage.text("높이 늘리기")
                onPressed: root.presentation.startInteraction()
                onClicked: root.presentation.changeHeight(80)
                background: Rectangle { radius: 5 * root.s.scale; color: root.s.ink; opacity: taller.enabled ? (taller.hovered ? 0.12 : 0.06) : 0.025; border.width: taller.visualFocus ? 2 : 0; border.color: root.s.accent }
                contentItem: Text { text: taller.text; color: root.s.ink; opacity: taller.enabled ? 1 : 0.35; font.pixelSize: 16 * root.s.scale; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            }
        }
    }
}
