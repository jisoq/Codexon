import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var node
    property int hoveredRow: -1
    implicitWidth: 250; implicitHeight: node.state.inline ? 70 + node.rowModel.rowCount() * node.state.rowHeight : 200
    function showDetails() {
        if (root.node.state.selected < 0) return
        detailsText.text = root.node.cellDetails(root.node.state.selected, table.keyboardColumn)
        details.open()
    }
    Rectangle { anchors.fill: parent; color: (appTheme.palette && appTheme.color("surface")); border.color: (appTheme.palette && appTheme.color("border")); radius: 6 }
    HorizontalHeaderView {
        id: header; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
        syncView: table; clip: true
        delegate: Rectangle {
            required property var display; required property int column; implicitWidth: 150; implicitHeight: 40
            color: (appTheme.palette && appTheme.color("secondary"))
            Text { id: headerLabel; anchors.fill: parent; anchors.margins: 9; text: appLanguage.text(display); elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; color: (appTheme.palette && appTheme.color("muted")); font.pixelSize: 14; font.weight: Font.Medium; font.family: appTheme.family }
            HoverHandler { id: headerHover }
            UiToolTip { visible: headerHover.hovered && headerLabel.truncated; text: appLanguage.text(display) }
            MouseArea {
                objectName: "column-resizer-" + column
                anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom; width: 8
                cursorShape: Qt.SplitHCursor; hoverEnabled:true
                Rectangle { anchors.fill:parent;color:appTheme.palette.accent;opacity:parent.containsMouse || parent.pressed ? .25 : 0 }
                property real startPosition
                property real startWidth
                onPressed: mouse => { startPosition=mapToItem(null,mouse.x,mouse.y).x; startWidth=root.node.visualColumnWidth(column) }
                onPositionChanged: mouse => { if (pressed) root.node.resizeColumn(column,startWidth+mapToItem(null,mouse.x,mouse.y).x-startPosition) }
            }
        }
    }
    TableView {
        id: table; objectName: "dataTable"; anchors.left: parent.left; anchors.right: parent.right; anchors.top: header.bottom; anchors.bottom: footer.top
        clip: true; model: root.node.rowModel; reuseItems: true; animate: false
        columnSpacing: 0; rowSpacing: 0
        columnWidthProvider: column => {
            let requested=root.node.visualColumnWidth(column)
            let exact=(root.node.state.noElideColumns || []).indexOf(root.node.logicalColumn(column)) >= 0
            if (exact) requested=Math.max(requested,table.implicitColumnWidth(column))
            let total=root.node.state.widths.reduce((sum,value) => sum+value,0)
            let extra=Math.max(0,table.width-total)
            if (column === (root.node.state.stretchVisual || 0)) requested += extra
            return column === 0 && !exact ? Math.min(requested,Math.max(100,table.width-12)) : requested
        }
        rowHeightProvider: row => root.node.state.rowHeight
        ScrollBar.horizontal: UiScrollBar { orientation: Qt.Horizontal }
        ScrollBar.vertical: UiScrollBar { orientation: Qt.Vertical; policy: root.node.state.inline ? ScrollBar.AlwaysOff : ScrollBar.AsNeeded }
        WheelHandler {
            enabled: !root.node.state.inline
            target: null
            onWheel: event => {
                let horizontal = event.angleDelta.x !== 0 || (event.modifiers & Qt.ShiftModifier)
                let pixels = horizontal ? event.pixelDelta.x : event.pixelDelta.y
                let angle = horizontal ? (event.angleDelta.x || event.angleDelta.y) : event.angleDelta.y
                let delta = pixels || angle / 120 * root.node.state.rowHeight * 3
                if (horizontal) table.contentX=Math.max(0,Math.min(table.contentWidth-table.width,table.contentX-delta))
                else table.contentY=Math.max(0,Math.min(table.contentHeight-table.height,table.contentY-delta))
                event.accepted=true
            }
        }
        activeFocusOnTab: true
        property int keyboardColumn: 0
        onContentYChanged: { root.node.verticalPosition.setValue(contentY); updateVisible() }
        onContentXChanged: root.node.horizontalPosition.setValue(contentX)
        onHeightChanged: updateVisible()
        onWidthChanged: forceLayout()
        onRowsChanged: updateVisible()
        onTopRowChanged: updateVisible()
        onBottomRowChanged: updateVisible()
        function updateVisible() {
            root.node.visibleRows(topRow,bottomRow)
        }
        delegate: Rectangle {
            required property int row; required property int column
            required property string display; required property string tooltip
            required property string cellBackground; required property real changedCell
            required property bool cacheZero
            required property bool verbatim
            required property real cellBar
            required property int alignment
            property bool exactValue: (root.node.state.noElideColumns || []).indexOf(root.node.logicalColumn(column)) >= 0
            implicitWidth: exactValue ? Math.max(70,cellLabel.implicitWidth+18) : 150
            implicitHeight: root.node.state.rowHeight
            color: root.node.state.selected === row ? (appTheme.palette && appTheme.color("secondary")) : cacheZero ? (root.hoveredRow === row ? (appTheme.palette && appTheme.color("warning_hover")) : (appTheme.palette && appTheme.color("warning_surface"))) : cellBackground || (root.hoveredRow === row ? (appTheme.palette && appTheme.color("secondary")) : (appTheme.palette && appTheme.color("surface")))
            Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; color: (appTheme.palette && appTheme.color("border")) }
            Rectangle { visible: root.node.state.selected === row && column === 0; width: 2; height: parent.height; color: appTheme.palette.accent }
            Rectangle { objectName: "cell-data-bar"; visible: !!root.node.state.dataBars && cellBar >= 0; anchors.left: parent.left; anchors.leftMargin: 6; anchors.verticalCenter: parent.verticalCenter; width: Math.min(140,Math.max(0,parent.width-12))*Math.max(0,cellBar); height: 14; radius: 2; color: appTheme.palette.accent; opacity: .16 }
            Text { id: cellLabel; objectName: "cell-label"; anchors.fill: parent; anchors.leftMargin: 9; anchors.rightMargin: 9; text: verbatim ? display : appLanguage.text(display); wrapMode: !exactValue && root.node.state.rowHeight > 40 ? Text.Wrap : Text.NoWrap; elide: exactValue ? Text.ElideNone : Text.ElideRight; horizontalAlignment: alignment & Qt.AlignRight ? Qt.AlignRight : Qt.AlignLeft; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.family: appTheme.family; font.features: { "tnum": 1 }; color: (appTheme.palette && appTheme.color("ink")) }
            Rectangle { anchors.fill: parent; color: (appTheme.palette && appTheme.color("accent")); opacity: changedCell * .18 }
            Rectangle { objectName: "cache-zero-marker"; visible: cacheZero && column === 0; anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; width: 3; color: (appTheme.palette && appTheme.color("warning")) }
            Rectangle { objectName:"row-hover-feedback";anchors.fill:parent;color:appTheme.palette.accent;opacity:root.hoveredRow===row ? .08 : 0 }
            HoverHandler { id: hover;cursorShape:Qt.PointingHandCursor;onHoveredChanged: { if(hovered) root.hoveredRow=row;else if(root.hoveredRow===row) root.hoveredRow=-1 } }
            UiToolTip { objectName: "cell-overflow-tip"; visible: hover.hovered && cellLabel.truncated; text: display }
            TapHandler { onTapped: { table.forceActiveFocus(); table.keyboardColumn=column; root.node.click(row,column) } }
            Accessible.role: Accessible.Cell; Accessible.name: display; Accessible.description: (cacheZero ? appLanguage.text("캐시 읽기 0 · ") : "") + tooltip
        }
        Keys.onPressed: event => {
            let row = root.node.state.selected
            if (event.key === Qt.Key_Down) row++
            else if (event.key === Qt.Key_Up) row--
            else if (event.key === Qt.Key_PageDown) row += Math.max(1,Math.floor(height/root.node.state.rowHeight)-1)
            else if (event.key === Qt.Key_PageUp) row -= Math.max(1,Math.floor(height/root.node.state.rowHeight)-1)
            else if (event.key === Qt.Key_Left || event.key === Qt.Key_Right) {
                keyboardColumn=Math.max(0,Math.min(columns-1,keyboardColumn+(event.key===Qt.Key_Left ? -1:1)))
                positionViewAtColumn(keyboardColumn,TableView.Contain);event.accepted=true;return
            }
            else if (event.key === Qt.Key_Home || event.key === Qt.Key_End) {
                keyboardColumn=event.key===Qt.Key_Home ? 0:columns-1
                positionViewAtColumn(keyboardColumn,TableView.Contain)
                row=event.key===Qt.Key_Home ? 0:rows-1
            }
            else if (event.key === Qt.Key_Return || event.key === Qt.Key_Space) { root.node.activateRow(row,keyboardColumn); event.accepted=true; return }
            else return
            row=Math.max(0,Math.min(rows-1,row)); root.node.selectRow(row); positionViewAtRow(row,TableView.Contain); event.accepted=true
        }
    }
    Rectangle {
        id: footer; anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
        height: 30; color: (appTheme.palette && appTheme.color("secondary"))
        Rectangle { anchors.top: parent.top; width: parent.width; height: 1; color: (appTheme.palette && appTheme.color("border")) }
        Text {
            anchors.left: parent.left; anchors.leftMargin: 9; anchors.verticalCenter: parent.verticalCenter
            text: table.rows.toLocaleString(Qt.locale(), "f", 0) + appLanguage.text("개")
            color: (appTheme.palette && appTheme.color("muted")); font.family: appTheme.family; font.pixelSize: 12
        }
        UiButton {
            objectName: "cell-details-button"
            visible: !root.node.state.rowDetails && !root.node.state.inline
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            text: appLanguage.text("셀 상세"); enabled: root.node.state.selected >= 0
            implicitHeight: 24; implicitWidth: 66; padding: 3
            font.family: appTheme.family; font.pixelSize: 12
            onClicked: root.showDetails()
        }
    }
    Text {
        anchors.centerIn: table; visible: table.rows === 0
        text: appLanguage.text("기록 없음"); color: (appTheme.palette && appTheme.color("muted")); font.family: appTheme.family; font.pixelSize: 14
    }
    Popup {
        id: details; objectName: "cell-details-popup"
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(540, parent ? parent.width - 48 : 540)
        height: Math.min(380, parent ? parent.height - 48 : 380)
        padding: 20; modal: true; focus: true
        background: Rectangle { color: (appTheme.palette && appTheme.color("surface")); radius: 8; border.color: (appTheme.palette && appTheme.color("border")) }
        contentItem: ColumnLayout {
            Text { text: appLanguage.text("셀 상세"); font.family: appTheme.family; font.pixelSize: 18; font.bold: true; color: (appTheme.palette && appTheme.color("ink")) }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                contentWidth: availableWidth
                TextArea {
                    id: detailsText; objectName: "cell-details-text"
                    readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                    textFormat: TextEdit.PlainText; color: (appTheme.palette && appTheme.color("ink"))
                    font.family: appTheme.family; font.pixelSize: 14
                    background: null
                }
            }
            UiButton { text: appLanguage.text("닫기"); Layout.alignment: Qt.AlignRight; onClicked: details.close() }
        }
    }
    Connections {
        target: root.node
        function onGeometryChanged() { table.forceLayout() }
        function onScrollRequested(row,column) { table.forceLayout(); table.positionViewAtCell(Qt.point(column,row),TableView.Contain) }
    }
    Connections { target: root.node.verticalPosition; function onChanged() { table.contentY = root.node.verticalPosition.position } }
    Connections { target: root.node.horizontalPosition; function onChanged() { table.contentX = root.node.horizontalPosition.position } }
}
