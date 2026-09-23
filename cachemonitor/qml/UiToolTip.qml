import QtQuick
import QtQuick.Controls.Basic

ToolTip {
    id: control
    delay: 850; timeout: 6000
    padding: 10
    width: Math.min(360, implicitWidth)
    contentItem: Text {
        text: control.text; color: (appTheme.palette && appTheme.color("#f8fafc")); wrapMode: Text.Wrap
        font.family: appTheme.family; font.pixelSize: 13
    }
    background: Rectangle { color: (appTheme.palette && appTheme.color("#27374b")); radius: 5 }
}
