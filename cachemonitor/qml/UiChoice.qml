import QtQuick
import QtQuick.Window
import QtQuick.Controls.Basic

ComboBox {
    id: control
    hoverEnabled: true
    HoverHandler { cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
    property string tooltipText: ""
    readonly property var selectedEntry: model && currentIndex >= 0 && currentIndex < model.length ? model[currentIndex] : null
    readonly property real longestLabelWidth: {
        let widest = 0
        for (let entry of model || [])
            widest = Math.max(widest, labelMetrics.advanceWidth(String(entry[control.textRole] || entry)))
        return widest
    }
    FontMetrics { id: labelMetrics; font: control.font }
    implicitWidth: 180; implicitHeight: 36
    leftPadding: 12; rightPadding: 32; topPadding: 8; bottomPadding: 8
    font.family: appTheme.family; font.pixelSize: 14
    contentItem: Text {
        id: selectedText
        text: control.displayText; font: control.font
        color: control.enabled ? (appTheme.palette && appTheme.color("ink")) : (appTheme.palette && appTheme.color("unknown"))
        verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
    }
    UiToolTip {
        visible: control.hovered && !control.popup.visible && text.length > 0
        text: control.tooltipText || (control.selectedEntry && control.selectedEntry.tooltip) || (selectedText.truncated ? control.displayText : "")
    }
    indicator: Item {
        x: control.width - width - 12; anchors.verticalCenter: parent.verticalCenter
        width: 12; height: 8
        Rectangle { x: 0; y: 3; width: 7; height: 1.5; rotation: 45; color: appTheme.palette.muted }
        Rectangle { x: 4.5; y: 3; width: 7; height: 1.5; rotation: -45; color: appTheme.palette.muted }
    }
    background: Rectangle {
        radius: 6; color: control.enabled ? (appTheme.palette && appTheme.color("surface")) : (appTheme.palette && appTheme.color("secondary"))
        border.color: control.activeFocus ? (appTheme.palette && appTheme.color("accent")) : control.hovered ? (appTheme.palette && appTheme.color("muted")) : (appTheme.palette && appTheme.color("border"))
        border.width: control.activeFocus ? 2 : 1
    }
    delegate: ItemDelegate {
        id: option
        HoverHandler { cursorShape: Qt.PointingHandCursor }
        width: control.popup.availableWidth
        implicitHeight: Math.max(36, contentItem.implicitHeight + topPadding + bottomPadding)
        leftPadding: 10; rightPadding: 10; topPadding: 8; bottomPadding: 8
        highlighted: control.highlightedIndex === index
        contentItem: Text {
            text: modelData[control.textRole] || modelData
            font: control.font; color: (appTheme.palette && appTheme.color("ink"))
            wrapMode: Text.Wrap
            verticalAlignment: Text.AlignVCenter
        }
        UiToolTip { visible: option.hovered && text.length > 0; text: modelData.tooltip || "" }
        background: Rectangle { Rectangle { anchors.fill:parent;radius:4;color:appTheme.palette.accent;opacity:option.hovered ? .09 : 0 } color: highlighted ? (appTheme.palette && appTheme.color("secondary")) : (appTheme.palette && appTheme.color("surface")); radius: 4 }
    }
    popup: Popup {
        objectName: "choice-popup"
        y: control.height + 4
        width: Math.min(Math.max(control.width, Math.min(560, control.longestLabelWidth + 36)), control.Window.window ? control.Window.window.width - 24 : 560)
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
