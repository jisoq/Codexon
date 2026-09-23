import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

RowLayout {
    id: root
    required property var node
    UiInput {
        id: field; text: root.node.state.text; implicitWidth: 118
        inputMask: "9999-99-99"; Accessible.name: appLanguage.text("날짜")
        onEditingFinished: root.node.edit(text)
    }
    UiButton { iconName: "calendar"; flat:true; implicitWidth: 36; Accessible.name: appLanguage.text("달력 열기"); onClicked: calendar.open() }
    Popup {
        id: calendar; y: root.height; modal: false; padding: 12
        property date selected: new Date(root.node.state.text + "T12:00:00")
        contentItem: ColumnLayout {
            RowLayout {
                UiButton { iconName: "left";flat:true; implicitWidth: 36;Accessible.name:appLanguage.text("이전 달"); onClicked: calendar.selected = new Date(grid.year, grid.month - 1, 1) }
                Label { text: grid.year + " / " + (grid.month+1); Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter }
                UiButton { iconName: "right";flat:true; implicitWidth: 36;Accessible.name:appLanguage.text("다음 달"); onClicked: calendar.selected = new Date(grid.year, grid.month + 1, 1) }
            }
            DayOfWeekRow { locale: grid.locale; Layout.fillWidth: true }
            MonthGrid {
                id: grid; month: calendar.selected.getMonth(); year: calendar.selected.getFullYear()
                locale: Qt.locale(appLanguage.locale); Layout.preferredWidth: 280; Layout.preferredHeight: 230
                delegate: Rectangle {
                    required property var model
                    implicitWidth:36;implicitHeight:32;radius:4
                    color:model.today ? appTheme.palette.secondary : "transparent"
                    Rectangle { anchors.fill:parent;radius:4;color:appTheme.palette.accent;opacity:dayHover.hovered ? .12 : 0 }
                    Text { anchors.centerIn:parent;text:model.day;font.family:appTheme.family;color:model.month===grid.month ? appTheme.palette.ink : appTheme.palette.muted }
                    HoverHandler { id:dayHover;cursorShape:Qt.PointingHandCursor }
                }
                onClicked: date => { root.node.edit(Qt.formatDate(date,"yyyy-MM-dd")); calendar.close() }
            }
        }
    }
}
