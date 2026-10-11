import QtQuick
import QtQuick.Controls

Flow {
    id: root
    required property var node
    spacing: 4
    Repeater {
        model: root.node.state.entries || []
        Row {
            id: crumb
            required property var modelData
            required property int index
            objectName: "history-crumb-" + index
            property string caption: modelData.literal ? modelData.text : appLanguage.text(modelData.text)
            spacing: 4; height: 36
            TextMetrics { id: metrics; text: crumb.caption; font.family: appTheme.family; font.pixelSize: 14 }
            Text { visible: crumb.index > 0; width: 12; height: 36; text: "›"; verticalAlignment: Text.AlignVCenter; font.pixelSize: 18; color: appTheme.palette.muted }
            AbstractButton {
                id: link
                width: Math.min(360, Math.max(0, root.width - 20), metrics.width + 12); height: 36
                enabled: !crumb.modelData.current
                activeFocusOnTab: enabled; hoverEnabled: true
                onClicked: root.node.activate(crumb.index)
                Keys.onReturnPressed: root.node.activate(crumb.index)
                contentItem: Text {
                    id: caption
                    text: crumb.caption; elide: Text.ElideRight; textFormat: Text.PlainText
                    font.family: appTheme.family; font.pixelSize: 14; font.weight: crumb.modelData.current ? Font.DemiBold : Font.Normal
                    verticalAlignment: Text.AlignVCenter
                    color: link.hovered || link.activeFocus ? appTheme.palette.accent : appTheme.palette.ink
                }
                background: Rectangle {
                    color: "transparent"; radius: 4
                    border.width: link.activeFocus ? 1 : 0; border.color: appTheme.palette.accent
                }
                HoverHandler { id: currentHover; cursorShape: link.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
                UiToolTip { visible: currentHover.hovered && caption.truncated; text: crumb.caption }
                Accessible.role: Accessible.Link; Accessible.name: crumb.caption
            }
        }
    }
}
