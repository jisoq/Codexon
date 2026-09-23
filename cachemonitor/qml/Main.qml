import QtQuick
import QtQuick.Controls

Rectangle {
    required property var presentation
    color: (appTheme.palette && appTheme.color("white"))
    NodeView { anchors.fill: parent; node: presentation }
}
