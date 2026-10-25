import QtQuick
import QtQuick.Controls
import CacheMonitor 6.0

Rectangle {
    id: root
    required property var presentation
    property var s: presentation.state
    // Native placement sets physical pixels; Qt may use a different logical scale.
    readonly property real popupScale: width / (s.popupKind === "settings" ? 260 : 160)
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
        UiToolTip { visible: moveHover.hovered && root.s.chromeKind === "icon"; text: appLanguage.text("오버레이 펼치기") }

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
        anchors.fill: moveTarget; radius: 6 * root.s.scale
        visible: root.s.chromeKind === "header"
        color: root.s.ink
        opacity: root.s.pressed ? 0.10 : root.s.hovered ? 0.06 : 0
    }

    Rectangle {
        anchors.fill: parent
        anchors.margins: root.outerMargin
        radius: root.s.chromeKind === "toolbar" ? 10 * root.popupScale : 8 * root.s.scale
        visible: root.s.chromeKind === "toolbar" || root.s.chromeKind === "icon"
        color: root.s.surface
        opacity: root.s.chromeKind === "toolbar" ? 1 : root.s.opacity / 100
        border.width: root.s.chromeKind === "toolbar" ? root.popupScale : root.s.scale; border.color: root.s.border
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
            x: 12 * root.popupScale; y: 8 * root.popupScale
            width: 96 * root.popupScale; height: 24 * root.popupScale
            from: 0; to: 80; stepSize: 1; value: 100 - root.s.opacity
            padding: 4 * root.popupScale; focusPolicy: Qt.NoFocus
            Accessible.name: appLanguage.text("배경 투명도")
            Accessible.description: Math.round(value) + "%"
            onPressedChanged: if (pressed) { root.presentation.startInteraction(); forceActiveFocus(Qt.MouseFocusReason); }
            onMoved: root.presentation.transparency(Math.round(value))
            background: Rectangle {
                x: opacitySlider.leftPadding; y: opacitySlider.height / 2 - height / 2
                width: opacitySlider.availableWidth; height: 2 * root.popupScale; radius: root.popupScale
                color: root.s.meta; opacity: 0.45
            }
            handle: Rectangle {
                x: opacitySlider.leftPadding + opacitySlider.visualPosition * (opacitySlider.availableWidth - width)
                y: opacitySlider.height / 2 - height / 2
                width: 10 * root.popupScale; height: width; radius: width / 2; color: root.s.accent
                Rectangle {
                    anchors.fill: parent; anchors.margins: -4 * root.popupScale
                    visible: opacitySlider.visualFocus; radius: width / 2; color: "transparent"
                    border.width: 2 * root.popupScale; border.color: root.s.accent
                }
            }
        }
        UiText {
            x: 116 * root.popupScale; y: 8 * root.popupScale
            width: 32 * root.popupScale; height: 24 * root.popupScale
            text: (100 - root.s.opacity) + "%"
            color: root.s.ink; font.family: root.s.family; font.pixelSize: 11 * root.popupScale
            font.features: { "tnum": 1 }
            horizontalAlignment: Text.AlignRight; verticalAlignment: Text.AlignVCenter
        }
    }
    component SettingButton: Button {
        id: button
        width: implicitContentWidth + 16 * root.popupScale
        height: 26 * root.popupScale
        padding: 0; hoverEnabled: true
        onPressed: { root.presentation.startInteraction(); forceActiveFocus(Qt.MouseFocusReason); }
        background: Rectangle {
            radius: 5 * root.popupScale
            color: button.checked ? root.s.accent : root.s.ink
            opacity: button.down ? .26 : button.hovered ? (button.checked ? .26 : .12) : button.checked ? .2 : .06
            border.width: button.visualFocus ? 2 * root.popupScale : 0
            border.color: root.s.accent
        }
        contentItem: UiText {
            text: button.text; color: root.s.ink
            font.family: root.s.family; font.pixelSize: 11 * root.popupScale
            verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter
        }
        Accessible.name: text
        Accessible.checked: checked
    }
    Item {
        anchors.fill: parent
        visible: root.s.chromeKind === "toolbar" && root.s.popupKind === "settings"
        UiText {
            x: 12 * root.popupScale; y: 10 * root.popupScale
            height: 26 * root.popupScale
            text: appLanguage.text("차트 표시 방식")
            color: root.s.ink; font.family: root.s.family; font.pixelSize: 11 * root.popupScale
            verticalAlignment: Text.AlignVCenter
        }
        Row {
            x: 108 * root.popupScale; y: 10 * root.popupScale
            spacing: 4 * root.popupScale
            Repeater {
                model: [{ key: "bar", label: "막대" }, { key: "filled-line", label: "채움 선형" }]
                delegate: SettingButton {
                    required property var modelData
                    objectName: "chartStyle-" + modelData.key
                    checkable: true; checked: root.s.chartStyle === modelData.key
                    text: appLanguage.text(modelData.label)
                    onClicked: root.presentation.setChartStyle(modelData.key)
                }
            }
        }
        UiText {
            x: 12 * root.popupScale; y: 42 * root.popupScale
            height: 26 * root.popupScale
            text: appLanguage.text("행 강조 방식")
            color: root.s.ink; font.family: root.s.family; font.pixelSize: 11 * root.popupScale
            verticalAlignment: Text.AlignVCenter
        }
        ComboBox {
            id: highlightChoice
            objectName: "highlightStyle"
            x: 108 * root.popupScale; y: 42 * root.popupScale
            width: 140 * root.popupScale; height: 26 * root.popupScale
            model: (root.s.highlightStyles || []).map(entry => ({key: entry.key, label: appLanguage.text(entry.label)}))
            textRole: "label"; valueRole: "key"
            currentIndex: model.findIndex(entry => entry.key === root.s.highlightStyle)
            leftPadding: 8 * root.popupScale; rightPadding: 22 * root.popupScale
            font.family: root.s.family; font.pixelSize: 11 * root.popupScale
            hoverEnabled: true
            onPressedChanged: if (pressed) { root.presentation.startInteraction(); forceActiveFocus(Qt.MouseFocusReason); }
            onActivated: root.presentation.setHighlightStyle(currentValue)
            Accessible.name: appLanguage.text("행 강조 방식")
            contentItem: UiText {
                text: highlightChoice.displayText; font: highlightChoice.font; color: root.s.ink
                verticalAlignment: Text.AlignVCenter
            }
            indicator: UiText {
                x: parent.width - 18 * root.popupScale; height: parent.height
                text: "⌄"; color: root.s.meta; font.pixelSize: 14 * root.popupScale
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: 5 * root.popupScale; color: root.s.ink
                opacity: highlightChoice.hovered ? .12 : .06
                border.width: highlightChoice.visualFocus ? 2 * root.popupScale : 0
                border.color: root.s.accent
            }
            delegate: ItemDelegate {
                id: option
                required property var modelData
                required property int index
                objectName: "highlightStyle-" + modelData.key
                width: highlightChoice.popup.availableWidth; height: 28 * root.popupScale
                highlighted: highlightChoice.highlightedIndex === index
                Accessible.name: modelData.label
                Accessible.checked: highlightChoice.currentIndex === index
                contentItem: UiText {
                    text: option.modelData.label; font: highlightChoice.font; color: root.s.ink
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: 4 * root.popupScale; color: root.s.accent
                    opacity: option.highlighted ? .2 : highlightChoice.currentIndex === option.index ? .1 : 0
                }
            }
            popup: Popup {
                objectName: "highlightStylePopup"
                popupType: Popup.Window
                y: highlightChoice.height + 4 * root.popupScale
                width: highlightChoice.width
                implicitWidth: highlightChoice.width
                implicitHeight: contentItem.implicitHeight + 8 * root.popupScale
                padding: 4 * root.popupScale
                contentItem: ListView {
                    clip: true; implicitHeight: contentHeight
                    model: highlightChoice.popup.visible ? highlightChoice.delegateModel : null
                    currentIndex: highlightChoice.highlightedIndex
                }
                background: Rectangle {
                    radius: 6 * root.popupScale; color: root.s.surface
                    border.width: root.popupScale; border.color: root.s.border
                }
            }
        }
        Rectangle {
            x: 12 * root.popupScale; y: 72 * root.popupScale
            width: 236 * root.popupScale; height: 32 * root.popupScale
            radius: 5 * root.popupScale; color: "transparent"
            border.width: root.popupScale; border.color: root.s.border
            QuickPlot {
                objectName: "highlightPreview"
                anchors.fill: parent
                source: root.presentation.highlightPreview
                activeFocusOnTab: false
                Accessible.role: Accessible.Graphic
                Accessible.name: appLanguage.text("행 강조 미리보기")
            }
            UiText {
                x: 10 * root.popupScale; anchors.verticalCenter: parent.verticalCenter
                text: appLanguage.text("미리보기")
                color: root.s.meta; font.family: root.s.family; font.pixelSize: 11 * root.popupScale
            }
        }
        UiText {
            x: 12 * root.popupScale; y: 114 * root.popupScale
            height: 26 * root.popupScale
            visible: root.s.canAdjustAgentCount || false
            text: appLanguage.text("서브에이전트 표시 개수")
            color: root.s.ink; font.family: root.s.family; font.pixelSize: 11 * root.popupScale
            verticalAlignment: Text.AlignVCenter
        }
        Row {
            x: 124 * root.popupScale; y: 114 * root.popupScale
            spacing: 4 * root.popupScale
            visible: root.s.canAdjustAgentCount || false
            Repeater {
                model: [1, 2, 3, 4]
                delegate: SettingButton {
                    required property int modelData
                    objectName: "agentCount-" + modelData
                    width: 28 * root.popupScale
                    checkable: true; autoExclusive: true
                    checked: (root.s.visibleAgentCount || 3) === modelData
                    text: String(modelData)
                    onClicked: root.presentation.setAgentCount(modelData)
                    Accessible.name: appLanguage.text("서브에이전트 표시 개수") + " " + text
                }
            }
        }
    }
}
