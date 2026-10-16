import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var node
    property var s: node.state
    property bool compact: !!s.compact
    property var stages: ["기록 준비", "기록 조회", "지표 계산", "화면 준비"]
    implicitWidth: compact ? 500 : 660
    implicitHeight: body.implicitHeight
    Accessible.role: Accessible.ProgressBar
    Accessible.name: appLanguage.text(s.title || "불러오는 중") + ": " + appLanguage.text(s.detail || "")
    Accessible.description: appLanguage.text("완료 시간은 아직 알 수 없습니다.")
    ColumnLayout {
        id: body
        width: Math.min(parent.width, 660)
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: root.compact ? 8 : 20
        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            Text {
                text: appLanguage.text(root.s.title || "불러오는 중")
                font.family: appTheme.family
                font.pixelSize: root.compact ? 14 : 20
                font.weight: Font.DemiBold
                color: appTheme.palette && appTheme.color("ink")
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                horizontalAlignment: root.compact ? Text.AlignLeft : Text.AlignHCenter
            }
        }
        Text {
            text: appLanguage.text(root.s.detail || "")
            font.family: appTheme.family
            font.pixelSize: 14
            color: appTheme.palette && appTheme.color("muted")
            wrapMode: Text.WordWrap
            horizontalAlignment: root.compact ? Text.AlignLeft : Text.AlignHCenter
            Layout.fillWidth: true
        }
        Rectangle {
            id: track
            Layout.fillWidth: true
            Layout.preferredHeight: 3
            color: appTheme.palette && appTheme.color("border")
            radius: 1.5
            clip: true
            Rectangle {
                id: runner
                width: track.width * 0.24
                height: track.height
                radius: 1.5
                color: appTheme.palette && appTheme.color("accent")
                NumberAnimation on x {
                    from: -runner.width; to: track.width
                    duration: 1450
                    loops: Animation.Infinite
                    running: root.visible && root.s.running !== false
                }
            }
        }
        GridLayout {
            visible: !root.compact && root.s.showStages !== false
            Layout.fillWidth: true
            columns: root.width < 560 ? 2 : 4
            rowSpacing: 14
            columnSpacing: 12
            Repeater {
                model: root.stages
                RowLayout {
                    required property int index
                    required property string modelData
                    Layout.fillWidth: true
                    spacing: 8
                    Rectangle {
                        width: 24; height: 24; radius: 12
                        border.width: index === root.s.stage ? 1 : 0
                        border.color: appTheme.palette && appTheme.color("accent")
                        color: appTheme.palette && appTheme.color(index <= root.s.stage ? "hover" : "transparent")
                        Text {
                            anchors.centerIn: parent
                            text: index < root.s.stage ? "✓" : String(index + 1)
                            font.family: appTheme.family
                            font.pixelSize: 12
                            color: appTheme.palette && appTheme.color(index === root.s.stage ? "accent" : "muted")
                        }
                    }
                    Text {
                        text: appLanguage.text(modelData)
                        font.family: appTheme.family
                        font.pixelSize: 13
                        font.weight: index === root.s.stage ? Font.DemiBold : Font.Normal
                        color: appTheme.palette && appTheme.color(index === root.s.stage ? "ink" : "muted")
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }
            }
        }
        Text {
            visible: !root.compact
            text: appLanguage.text(root.s.slow ? "기록 처리에 시간이 걸리고 있습니다. 다른 탭을 이용할 수 있습니다." : "다른 탭을 이용할 수 있습니다.")
            wrapMode: Text.WordWrap
            font.family: appTheme.family
            font.pixelSize: 12
            color: appTheme.palette && appTheme.color("muted")
            horizontalAlignment: Text.AlignHCenter
            Layout.fillWidth: true
        }
    }
}
