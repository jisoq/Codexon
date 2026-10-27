import QtQuick

Rectangle {
    id: swatch
    property string colorKey: ""
    readonly property bool striped: colorKey.startsWith("model-mode:Fast:")
    width: 12; height: 12
    color: colorKey ? (appTheme.palette && appTheme.color(colorKey)) : "transparent"
    clip: true

    Repeater {
        model: swatch.striped ? Math.ceil((swatch.width + swatch.height) / 8) + 1 : 0
        delegate: Rectangle {
            required property int index
            x: index * 8 - swatch.height; y: 0
            width: 1; height: swatch.height * Math.SQRT2
            rotation: -45; transformOrigin: Item.TopLeft
            color: "white"
            antialiasing: true
        }
    }
}
