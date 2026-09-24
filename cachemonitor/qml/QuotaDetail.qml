import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: card
    objectName: shareCard ? "quotaShareDetailCard" : "quotaDetailCard"
    required property var plot
    property bool shareCard: false
    property var primaryCard: null
    readonly property var detailData: shareCard ? (plot.detail.share || {}) : plot.detail
    property real companionHeight: 0
    parent: plot.Window.window ? plot.Window.window.contentItem : null
    visible: plot.visible && !!detailData.title
    property real padding: 10
    readonly property real availableWidth: width - padding * 2
    height: cardContent.implicitHeight + padding * 2
    z: 1000
    width: Math.min(340, (parent ? parent.width : 404) - 24)
    property point anchor: plot.mapToItem(parent, plot.detailX, plot.detailY)
    readonly property real chartTop: plot.mapToItem(parent, 0, plot.detail.chart_top || 0).y
    readonly property real chartBottom: plot.mapToItem(parent, 0, plot.detail.chart_bottom || plot.height).y
    x: primaryCard ? primaryCard.x : Math.max(8, Math.min((parent ? parent.width : 404) - width - 8,
                           anchor.x + width + 20 < (parent ? parent.width : 404) ? anchor.x + 20 : anchor.x - width - 20))
    y: primaryCard ? Math.max(primaryCard.y + primaryCard.height + 8,
                             Math.min((parent ? parent.height : 800) - height - 8,
                                      plot.mapToItem(parent, plot.detailX, plot.detail.strip_bottom || plot.height).y + 12))
                  : Math.max(8, Math.min((parent ? parent.height : 800) - height - companionHeight - 8,
                           plot.detail.share ? Math.max(chartTop, Math.min(anchor.y + 16, chartBottom - height - 12)) :
                           anchor.y + height + 16 < (parent ? parent.height : 800) ? anchor.y + 16 : anchor.y - height - 16))
    color: appTheme.palette.surface
    border.color: appTheme.palette.accent
    border.width: 1
    radius: 8
    ColumnLayout {
        id: cardContent
        x: card.padding; y: card.padding; width: card.availableWidth
        spacing: 5
        RowLayout {
            Layout.fillWidth: true
            Text {
                text: card.detailData.title || ""
                font.family: appTheme.family; font.pixelSize: 15; font.bold: true
                color: appTheme.palette.ink; wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            ToolButton {
                objectName: "quotaDetailClose"
                visible: card.plot.detailPinned && !card.shareCard
                text: "×"; font.pixelSize: 18
                padding: 0
                Layout.preferredWidth: 22; Layout.preferredHeight: 22
                contentItem: Text {
                    text: "×"; font.pixelSize: 18; color: appTheme.palette.ink
                    horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                }
                Accessible.name: appLanguage.text("상세 카드 닫기")
                onClicked: card.plot.dismissDetail()
                background: Rectangle { radius: 4; color: parent.hovered ? appTheme.palette.hover : "transparent" }
            }
        }
        Repeater {
            model: card.detailData.items || []
            delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                spacing: 10
                Rectangle {
                    visible: !!modelData.model
                    Layout.preferredWidth: visible ? 10 : 0
                    Layout.preferredHeight: 10
                    color: modelData.model ? (appTheme.palette && appTheme.color("model:" + modelData.model)) : "transparent"
                }
                Text {
                    text: appLanguage.text(modelData.label)
                    font.family: appTheme.family; font.pixelSize: 13
                    color: modelData.model ? appTheme.palette.ink : (appTheme.palette && appTheme.readableText(modelData.color, "surface"))
                    wrapMode: Text.Wrap; Layout.fillWidth: true
                }
                Text {
                    objectName: "quotaDetailValue"
                    text: modelData.value
                    font.family: appTheme.family; font.pixelSize: 15; font.bold: true
                    color: appTheme.palette.ink
                    wrapMode: Text.Wrap; horizontalAlignment: Text.AlignRight
                    Layout.maximumWidth: card.availableWidth * 0.57
                }
            }
        }
        Text {
            text: appLanguage.text(card.detailData.note || "")
            visible: text.length > 0
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            font.family: appTheme.family; font.pixelSize: 13
            color: appTheme.palette.muted
        }
    }
}
