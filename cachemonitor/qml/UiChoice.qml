import QtQuick
import QtQuick.Window
import QtQuick.Controls.Basic

ComboBox {
    id: control
    hoverEnabled: true
    HoverHandler { cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
    property string tooltipText: ""
    property bool wrapSelectedText: false
    readonly property var selectedEntry: model && currentIndex >= 0 && currentIndex < model.length ? model[currentIndex] : null
    readonly property real longestLabelWidth: {
        let widest = 0
        for (let entry of model || [])
            widest = Math.max(widest, labelMetrics.advanceWidth(String(entry[control.textRole] || entry)))
        return widest
    }
    FontMetrics { id: labelMetrics; font: control.font }
    implicitWidth: 180; implicitHeight: Math.max(36, selectedText.contentHeight + topPadding + bottomPadding)
    leftPadding: 10; rightPadding: 40; topPadding: 8; bottomPadding: 8
    font.family: appTheme.family; font.pixelSize: 14
    contentItem: Text {
        id: selectedText
        text: control.displayText; font: control.font
        color: control.enabled ? (appTheme.palette && appTheme.color((control.selectedEntry && control.selectedEntry.ink) || "ink")) : (appTheme.palette && appTheme.color("unknown"))
        verticalAlignment: Text.AlignVCenter
        wrapMode: control.wrapSelectedText ? Text.Wrap : Text.NoWrap
        elide: control.wrapSelectedText ? Text.ElideNone : Text.ElideRight
    }
    UiToolTip {
        visible: control.hovered && !control.popup.visible && text.length > 0
        text: control.tooltipText || (control.selectedEntry && control.selectedEntry.tooltip) || (selectedText.truncated ? control.displayText : "")
    }
    indicator: Item {
        x: control.width - width; anchors.verticalCenter: parent.verticalCenter
        width: 32; height: 24
        Rectangle { anchors.fill: parent; radius: 3; color: appTheme.palette.secondary; opacity: control.enabled ? 1 : .55 }
        Rectangle { width: 1; height: 16; anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; color: appTheme.palette.border }
        UiIcon { name: "down"; anchors.centerIn: parent; width: 18; height: 18; rotation: control.popup.visible ? 180 : 0; ink: control.enabled ? appTheme.palette.ink : appTheme.palette.unknown }
    }
    background: Rectangle {
        color: "transparent"
        Rectangle { anchors.fill: parent; color: appTheme.palette.accent; opacity: control.enabled && (control.hovered || control.popup.visible) ? .04 : 0 }
        Rectangle {
            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
            height: control.activeFocus || control.popup.visible ? 2 : 1
            color: control.activeFocus || control.popup.visible ? appTheme.palette.accent : control.hovered ? appTheme.palette.muted : appTheme.palette.border
            opacity: control.enabled ? 1 : .55
        }
    }
    delegate: ItemDelegate {
        id: option
        readonly property bool selected: control.currentIndex === index
        HoverHandler { cursorShape: Qt.PointingHandCursor }
        width: control.popup.availableWidth
        implicitHeight: Math.max(36, contentItem.implicitHeight + topPadding + bottomPadding)
        leftPadding: 32; rightPadding: 10; topPadding: 8; bottomPadding: 8
        highlighted: control.highlightedIndex === index
        contentItem: Text {
            text: modelData[control.textRole] || modelData
            font.family: control.font.family; font.pixelSize: control.font.pixelSize
            font.weight: option.selected ? Font.DemiBold : Font.Normal
            color: (appTheme.palette && appTheme.color(modelData.ink || "ink"))
            wrapMode: Text.Wrap
            verticalAlignment: Text.AlignVCenter
        }
        UiIcon { parent: option; visible: option.selected; name: "check"; x: 9; anchors.verticalCenter: parent.verticalCenter; width: 16; height: 16; ink: appTheme.palette.accent }
        UiToolTip { visible: option.hovered && text.length > 0; text: modelData.tooltip || "" }
        background: Rectangle { Rectangle { anchors.fill:parent;radius:4;color:appTheme.palette.accent;opacity:option.hovered ? .09 : 0 } color: highlighted ? (appTheme.palette && appTheme.color("secondary")) : (appTheme.palette && appTheme.color("surface")); radius: 4 }
    }
    popup: Popup {
        objectName: "choice-popup"
        y: control.height + 4
        width: Math.min(Math.max(control.width, Math.min(560, control.longestLabelWidth + 58)), control.Window.window ? control.Window.window.width - 24 : 560)
        margins: 12
        implicitHeight: Math.min(contentItem.implicitHeight + 8, 320)
        padding: 4
        contentItem: ListView {
            clip: true; implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator {
                contentItem: Rectangle { implicitWidth: 4; implicitHeight: 20; radius: 2; color: appTheme.palette.muted }
            }
        }
        background: Rectangle { color: (appTheme.palette && appTheme.color("surface")); radius: 6; border.color: (appTheme.palette && appTheme.color("border")) }
    }
}
