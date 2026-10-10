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
            UiButton {
                visible: !crumb.modelData.current
                width: Math.min(360, Math.max(0, root.width - 20), metrics.width + 36)
                text: crumb.caption; flat: true
                onClicked: root.node.activate(crumb.index)
                UiToolTip { visible: parent.hovered && metrics.width + 36 > parent.width; text: crumb.caption }
            }
            Text {
                visible: crumb.modelData.current
                width: Math.min(360, Math.max(0, root.width - 20), metrics.width + 12); height: 36
                text: crumb.caption; elide: Text.ElideRight; textFormat: Text.PlainText
                font.family: appTheme.family; font.pixelSize: 14; font.weight: Font.DemiBold
                verticalAlignment: Text.AlignVCenter; color: appTheme.palette.ink
                HoverHandler { id: currentHover }
                UiToolTip { visible: currentHover.hovered && parent.truncated; text: crumb.caption }
                Accessible.role: Accessible.StaticText; Accessible.name: text
            }
        }
    }
}
