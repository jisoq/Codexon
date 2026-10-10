import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var node
    spacing: 12
    Repeater {
        model: root.node.state.fields || []
        RowLayout {
            required property var modelData
            Layout.fillWidth: true; spacing: 18
            Text {
                Layout.preferredWidth: Math.min(140, root.width * .32); Layout.alignment: Qt.AlignTop
                text: appLanguage.text(modelData.label); wrapMode: Text.Wrap; textFormat: Text.PlainText
                font.family: appTheme.family; font.pixelSize: 14; color: appTheme.palette.muted
            }
            TextEdit {
                objectName: "record-field-value"
                Layout.fillWidth: true; Layout.minimumWidth: 0
                text: modelData.value; textFormat: TextEdit.PlainText; wrapMode: TextEdit.WrapAnywhere
                readOnly: true; selectByMouse: true; activeFocusOnTab: true
                font.family: appTheme.family; font.pixelSize: 14; color: appTheme.palette.ink
                selectionColor: appTheme.palette.selection; selectedTextColor: appTheme.palette.ink
            }
        }
    }
    TextEdit {
        Layout.fillWidth: true; Layout.minimumWidth: 0; visible: text.length > 0
        text: appLanguage.text(root.node.state.note || ""); textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap
        readOnly: true; selectByMouse: true; activeFocusOnTab: true
        font.family: appTheme.family; font.pixelSize: 14; color: appTheme.palette.ink
        selectionColor: appTheme.palette.selection; selectedTextColor: appTheme.palette.ink
    }
}
