import QtQuick
import QtQuick.Controls.Basic

ToolTip {
    id: control
    delay: 500; timeout: -1
    popupType: Popup.Window
    leftPadding: 8; rightPadding: 8
    topPadding: 6; bottomPadding: 6
    width: Math.min(280, implicitWidth)
    contentItem: Text {
        text: control.text; textFormat: Text.PlainText; color: (appTheme.palette && appTheme.color("tooltip_ink")); wrapMode: Text.Wrap
        font.family: appTheme.family; font.pixelSize: 13
    }
    background: Rectangle { color: (appTheme.palette && appTheme.color("tooltip_surface")); radius: 4 }
}
