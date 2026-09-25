import QtQuick
import QtQuick.Controls

Rectangle {
    required property var presentation
    color: (appTheme.palette && appTheme.color("surface"))
    NodeView { anchors.fill: parent; node: presentation }
}
