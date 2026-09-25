import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    required property var presentation
    color: appTheme.palette.hit_surface
    RowLayout {
        anchors.centerIn: parent; spacing: 4
        Image { source: root.presentation.state.logo; Layout.preferredWidth: 18; Layout.preferredHeight: 18; sourceSize.width: 36; sourceSize.height: 36 }
        Text { text: root.presentation.state.caption; color: root.presentation.state.foreground; font.family: "Pretendard JP"; font.pixelSize: 14 }
        Text { text: root.presentation.state.value; color: root.presentation.state.valueColor; font.family: "Pretendard JP"; font.pixelSize: 14; font.weight: Font.DemiBold }
    }
    Accessible.name: presentation.state.caption + appLanguage.text(" 잔여량 ") + presentation.state.value
}
