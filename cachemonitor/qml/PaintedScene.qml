import QtQuick
import CacheMonitor 6.0

Item {
    required property var presentation
    QuickPlot { anchors.fill: parent; source: presentation }
}
