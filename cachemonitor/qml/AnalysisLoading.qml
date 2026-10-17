import QtQuick

Item {
    id: root
    required property var status
    objectName: "analysis-region-loading"
    visible: !!status.active
    clip: true
    readonly property bool retained: !!status.retained
    readonly property string slowDetail: appLanguage.text("기록 처리에 시간이 걸리고 있습니다. 다른 탭을 이용할 수 있습니다.")
    Rectangle {
        anchors.fill: parent
        visible: !root.retained
        color: appTheme.palette && appTheme.color("surface")
    }
    // The data stays mounted. Only an unrelated result is hidden by its owner.
    UiLoading {
        anchors.top: parent.top
        anchors.topMargin: root.retained ? 0 : Math.max(12, (parent.height-height)/2)
        anchors.horizontalCenter: parent.horizontalCenter
        width: root.retained ? parent.width : Math.max(0, Math.min(parent.width-24, 440))
        height: implicitHeight
        node: ({state: {compact: true, minimal: root.retained, running: root.status.running,
                       title: root.status.title, detail: root.retained ? "" : root.status.slow ? root.slowDetail : (root.status.detail || "")}})
        Accessible.description: root.status.slow ? root.slowDetail : appLanguage.text("완료 시간은 아직 알 수 없습니다.")
        HoverHandler { id: delayHover }
        UiToolTip { visible: delayHover.hovered && !!root.status.slow; text: root.slowDetail }
        Rectangle {
            visible: !root.retained
            z: -1; anchors.fill: parent; anchors.margins: -4
            color: appTheme.palette && appTheme.color("surface")
            radius: 4
        }
    }
}
