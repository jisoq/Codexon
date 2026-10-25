import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var node
    spacing: 12
    function fieldText(field) {
        if (!field.textRuns || !field.textRuns.length) return field.value
        return field.textRuns.map(run => {
            let value=String(run.text).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")
            return run.color ? '<font color="'+(appTheme.palette && appTheme.color(run.color))+'">'+value+'</font>' : value
        }).join("")
    }
    Repeater {
        model: root.node.state.fields || []
        RowLayout {
            required property var modelData
            Layout.fillWidth: true; spacing: 18
            UiText {
                Layout.preferredWidth: Math.min(140, root.width * .32); Layout.alignment: Qt.AlignTop
                text: appLanguage.text(modelData.label); wrapMode: Text.Wrap; textFormat: Text.PlainText
                font.family: appTheme.family; font.pixelSize: 14; color: appTheme.palette && root.node.state.readableLabels ? appTheme.readableText("muted","surface") : appTheme.palette.muted
            }
            TextEdit {
                objectName: "record-field-value"
                Layout.fillWidth: true; Layout.minimumWidth: 0
                text: root.fieldText(modelData); textFormat: modelData.textRuns && modelData.textRuns.length ? TextEdit.RichText : TextEdit.PlainText; wrapMode: TextEdit.WrapAnywhere
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
