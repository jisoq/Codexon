import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var node
    readonly property var viewState: node.state
    readonly property bool readableColumns: !!viewState.readableColumns
    readonly property real cellPadding: readableColumns ? 12 : 9
    readonly property bool hierarchical: !!viewState.hierarchy
    readonly property bool frozen: !!viewState.freezeFirstColumn
    readonly property real frozenWidth: frozen ? columnWidth(0) : 0
    function columnWidth(column) {
        let requested=root.node.visualColumnWidth(column)
        let total=root.viewState.widths.reduce((sum,value) => sum+value,0)
        let extra=Math.max(0,root.width-total)
        let weights=root.viewState.columnStretchWeights || []
        if (weights.length === root.viewState.widths.length) requested += extra * weights[column] / Math.max(1,weights.reduce((sum,value) => sum+value,0))
        else if (column === (root.viewState.stretchVisual || 0)) requested += extra
        return frozen && column===0 ? Math.min(requested,Math.max(100,root.width-140)) : requested
    }
    function columnAlignment(column) {
        return (root.viewState.leftColumns || [0]).indexOf(root.node.logicalColumn(column)) >= 0 ? Text.AlignLeft : Text.AlignRight
    }
    property int hoveredRow: -1
    implicitWidth: 250; implicitHeight: root.viewState.inline ? 70 + node.rowModel.rowCount() * root.viewState.rowHeight : 200
    function showDetails() {
        if (root.viewState.selected < 0) return
        detailsText.text = root.node.cellDetails(root.viewState.selected, table.keyboardColumn)
        details.open()
    }
    Rectangle { anchors.fill: parent; color: (appTheme.palette && appTheme.color("surface")); border.color: root.viewState.treeNavigation ? "transparent" : (appTheme.palette && appTheme.color("border")); radius: 6 }
    HorizontalHeaderView {
        id: header; visible: !root.viewState.treeNavigation; height: root.viewState.treeNavigation ? 0 : 40; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
        anchors.leftMargin: root.frozenWidth
        syncView: table; clip: true
        delegate: headerDelegate
    }
    HorizontalHeaderView {
        visible: root.frozen; height: 40; width: root.frozenWidth; anchors.left: parent.left; anchors.top: parent.top
        syncView: firstColumn; clip: true; delegate: headerDelegate
    }
    Component {
        id: headerDelegate
        Rectangle {
            required property var display; required property int column; implicitWidth: 150; implicitHeight: 40
            color: (appTheme.palette && appTheme.color("secondary"))
            Text { id: headerLabel; objectName: "table-header-label"; property int textAlignment: horizontalAlignment; anchors.fill: parent; anchors.topMargin: 9; anchors.bottomMargin: 9; anchors.leftMargin: root.hierarchical && column===0 ? 50 : root.cellPadding; anchors.rightMargin: root.cellPadding; text: appLanguage.text(display); elide: Text.ElideRight; horizontalAlignment: root.readableColumns ? root.columnAlignment(column) : Text.AlignLeft; verticalAlignment: Text.AlignVCenter; color: (appTheme.palette && appTheme.color(root.readableColumns ? "ink" : "muted")); font.pixelSize: 14; font.weight: root.readableColumns ? Font.DemiBold : Font.Medium; font.family: appTheme.family }
            Rectangle { anchors.right: parent.right; width: 1; height: parent.height; visible: root.readableColumns; color: appTheme.palette.border }
            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; visible: root.readableColumns; color: appTheme.palette.border }
            HoverHandler { id: headerHover }
            UiToolTip {
                property string explanation: (root.viewState.headerTips || {})[column] || ""
                visible: headerHover.hovered && (explanation.length > 0 || headerLabel.truncated)
                text: appLanguage.text(explanation || display)
            }
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
        anchors.leftMargin: root.frozenWidth
        clip: true; model: root.node.rowModel; reuseItems: true; animate: false
        columnSpacing: 0; rowSpacing: 0
        columnWidthProvider: column => {
            if (root.frozen && column===0) return 0
            if (root.frozen) {
                let requested=root.columnWidth(column)
                if ((root.viewState.noElideColumns || []).indexOf(root.node.logicalColumn(column)) >= 0) requested=Math.max(requested,table.implicitColumnWidth(column))
                return requested
            }
            let requested=root.node.visualColumnWidth(column)
            let exact=(root.viewState.noElideColumns || []).indexOf(root.node.logicalColumn(column)) >= 0
            if (exact) requested=Math.max(requested,table.implicitColumnWidth(column))
            let total=root.viewState.widths.reduce((sum,value) => sum+value,0)
            let extra=Math.max(0,table.width-total)
            let weights=root.viewState.columnStretchWeights || []
            if (weights.length === root.viewState.widths.length) requested += extra * weights[column] / Math.max(1,weights.reduce((sum,value) => sum+value,0))
            else if (column === (root.viewState.stretchVisual || 0)) requested += extra
            return column === 0 && !exact ? Math.min(requested,Math.max(100,table.width-12)) : requested
        }
        rowHeightProvider: row => root.viewState.rowHeight
        ScrollBar.horizontal: UiScrollBar { orientation: Qt.Horizontal }
        ScrollBar.vertical: UiScrollBar { orientation: Qt.Vertical; policy: root.viewState.inline ? ScrollBar.AlwaysOff : ScrollBar.AsNeeded }
        WheelHandler {
            enabled: !root.viewState.inline
            target: null
            onWheel: event => {
                let horizontal = event.angleDelta.x !== 0 || (event.modifiers & Qt.ShiftModifier)
                let pixels = horizontal ? event.pixelDelta.x : event.pixelDelta.y
                let angle = horizontal ? (event.angleDelta.x || event.angleDelta.y) : event.angleDelta.y
                let delta = pixels || angle / 120 * root.viewState.rowHeight * 3
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
        delegate: cellDelegate
        Keys.onPressed: event => root.handleKey(event)
    }
    TableView {
        id: firstColumn; objectName: "frozen-column"
        visible: root.frozen; width: root.frozenWidth; anchors.left: parent.left; anchors.top: header.bottom; anchors.bottom: footer.top
        clip: true; model: root.frozen ? root.node.rowModel : null; reuseItems: true; animate: false
        syncView: root.frozen ? table : null; syncDirection: Qt.Vertical
        columnWidthProvider: column => column===0 ? root.frozenWidth : 0
        rowHeightProvider: row => root.viewState.rowHeight
        delegate: cellDelegate
        WheelHandler {
            target: null
            onWheel: event => {
                let delta=event.pixelDelta.y || event.angleDelta.y/120*root.viewState.rowHeight*3
                table.contentY=Math.max(0,Math.min(table.contentHeight-table.height,table.contentY-delta));event.accepted=true
            }
        }
    }
    Component {
        id: cellDelegate
        Rectangle {
            id: cell
            required property int row; required property int column
            required property string display; required property string tooltip
            required property string cellBackground; required property real changedCell
            required property bool cacheZero
            required property bool verbatim
            required property real cellBar
            required property int alignment
            required property var hierarchy
            readonly property var branch: hierarchy || ({})
            readonly property int depth: branch.depth || 0
            property bool exactValue: (root.viewState.noElideColumns || []).indexOf(root.node.logicalColumn(column)) >= 0
            implicitWidth: exactValue ? Math.max(70,cellLabel.implicitWidth+2*root.cellPadding) : 150
            implicitHeight: root.viewState.rowHeight
            color: root.viewState.selected === row || root.hierarchical && depth>0 ? appTheme.palette.secondary : cacheZero ? (root.hoveredRow === row ? appTheme.palette.warning_hover : appTheme.palette.warning_surface) : cellBackground || (root.hoveredRow === row ? appTheme.palette.secondary : appTheme.palette.surface)
            Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; visible: !root.viewState.treeNavigation; color: (appTheme.palette && appTheme.color("border")) }
            Rectangle { visible: root.viewState.selected === row && column === 0; width: 2; height: parent.height; color: appTheme.palette.accent }
            Rectangle { objectName: "cell-data-bar"; visible: !!root.viewState.dataBars && cellBar >= 0; anchors.left: parent.left; anchors.leftMargin: 6; anchors.verticalCenter: parent.verticalCenter; width: Math.min(140,Math.max(0,parent.width-12))*Math.max(0,cellBar); height: 14; radius: 2; color: appTheme.palette.accent; opacity: .16 }
            Text { id: cellLabel; objectName: "cell-label"; property int textAlignment: horizontalAlignment; anchors.fill: parent; anchors.leftMargin: root.hierarchical && column===0 ? 50+cell.depth*32 : root.cellPadding; anchors.rightMargin: root.cellPadding; text: verbatim ? display : appLanguage.text(display); wrapMode: !root.hierarchical && !exactValue && root.viewState.rowHeight > 40 ? Text.Wrap : Text.NoWrap; elide: exactValue ? Text.ElideNone : Text.ElideRight; horizontalAlignment: alignment & Qt.AlignRight ? Qt.AlignRight : Qt.AlignLeft; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14; font.family: appTheme.family; font.features: { "tnum": 1 }; color: (appTheme.palette && appTheme.color("ink")) }
            Item {
                visible: root.hierarchical && cell.column===0; anchors.fill: parent
                Repeater {
                    model: cell.branch.guides || []
                    Rectangle { required property bool modelData; required property int index; visible: modelData; x: 28+index*32; width: 2; height: cell.height; color: appTheme.palette.muted; opacity: .5 }
                }
                Rectangle { visible: cell.depth>0; x: 28+(cell.depth-1)*32; width: 2; height: cell.branch.last ? cell.height/2 : cell.height; color: appTheme.palette.muted; opacity: .5 }
                Rectangle { visible: cell.depth>0; x: 28+(cell.depth-1)*32; y: cell.height/2; height: 2; width: cell.branch.expandable ? 18 : 46; color: appTheme.palette.muted; opacity: .5 }
                Rectangle { visible: !!cell.branch.expandable && !!cell.branch.expanded; x: 28+cell.depth*32; y: cell.height/2+14; width: 2; height: cell.height/2-14; color: appTheme.palette.muted; opacity: .5 }
            }
            Rectangle { anchors.fill: parent; color: (appTheme.palette && appTheme.color("accent")); opacity: changedCell * .18 }
            Rectangle { objectName: "cache-zero-marker"; visible: cacheZero && column === 0; anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; width: 3; color: (appTheme.palette && appTheme.color("warning")) }
            Rectangle { objectName:"row-hover-feedback";anchors.fill:parent;color:appTheme.palette.accent;opacity:root.hoveredRow===row ? .08 : 0 }
            Rectangle { objectName: "column-divider"; anchors.right: parent.right; width: 1; height: parent.height; visible: root.readableColumns && !root.viewState.treeNavigation; color: appTheme.palette.border; opacity: .55 }
            HoverHandler { id: hover;cursorShape:root.viewState.navigationColumn !== undefined && column !== root.viewState.navigationColumn ? Qt.ArrowCursor : Qt.PointingHandCursor;onHoveredChanged: { if(hovered) root.hoveredRow=row;else if(root.hoveredRow===row) root.hoveredRow=-1 } }
            UiToolTip { objectName: "cell-overflow-tip"; visible: hover.hovered && cellLabel.truncated; text: display }
            TapHandler {
                onTapped: eventPoint => {
                    if (disclosure.visible && eventPoint.position.x >= disclosure.x && eventPoint.position.x <= disclosure.x+disclosure.width) return
                    table.forceActiveFocus(); table.keyboardColumn=column; root.node.click(row,column)
                }
            }
            UiButton {
                id: disclosure; objectName: "session-disclosure-"+cell.row
                visible: root.hierarchical && cell.column===0 && cell.width>0 && !!cell.branch.expandable
                x: 14+cell.depth*32; anchors.verticalCenter: parent.verticalCenter; width: 28; height: 28; flat: true
                Accessible.name: cell.display + " " + appLanguage.text(cell.branch.expanded ? "접기" : "펼치기")
                contentItem: Item {
                    Item {
                        anchors.centerIn: parent; width: 16; height: 16; rotation: cell.branch.expanded ? 90 : 0
                        Rectangle { x: 5; y: 3; width: 8; height: 2; rotation: 45; color: appTheme.palette.ink; radius: 1 }
                        Rectangle { x: 5; y: 8; width: 8; height: 2; rotation: -45; color: appTheme.palette.ink; radius: 1 }
                    }
                }
                onClicked: { root.node.toggleRow(cell.row); table.forceActiveFocus() }
                Keys.onLeftPressed: { root.node.selectRow(cell.row); root.node.expandCurrent(false); table.forceActiveFocus() }
                Keys.onRightPressed: { root.node.selectRow(cell.row); root.node.expandCurrent(true); table.forceActiveFocus() }
            }
            Accessible.role: Accessible.Cell; Accessible.name: display; Accessible.description: (cacheZero ? appLanguage.text("캐시 읽기 0: ") : "") + tooltip
        }
    }
    function handleKey(event) {
            let row = root.viewState.selected
            if (root.hierarchical && (event.key === Qt.Key_Left || event.key === Qt.Key_Right)) {
                root.node.expandCurrent(event.key === Qt.Key_Right); event.accepted=true; return
            }
            if (event.key === Qt.Key_Down) row++
            else if (event.key === Qt.Key_Up) row--
            else if (event.key === Qt.Key_PageDown) row += Math.max(1,Math.floor(table.height/root.viewState.rowHeight)-1)
            else if (event.key === Qt.Key_PageUp) row -= Math.max(1,Math.floor(table.height/root.viewState.rowHeight)-1)
            else if (event.key === Qt.Key_Left || event.key === Qt.Key_Right) {
                table.keyboardColumn=Math.max(0,Math.min(table.columns-1,table.keyboardColumn+(event.key===Qt.Key_Left ? -1:1)))
                if (!root.frozen || table.keyboardColumn>0) table.positionViewAtColumn(table.keyboardColumn,TableView.Contain);event.accepted=true;return
            }
            else if (event.key === Qt.Key_Home || event.key === Qt.Key_End) {
                table.keyboardColumn=event.key===Qt.Key_Home ? 0:table.columns-1
                if (!root.frozen || table.keyboardColumn>0) table.positionViewAtColumn(table.keyboardColumn,TableView.Contain)
                row=event.key===Qt.Key_Home ? 0:table.rows-1
            }
            else if (event.key === Qt.Key_Return || event.key === Qt.Key_Space) { root.node.activateRow(row,table.keyboardColumn); event.accepted=true; return }
            else return
            row=Math.max(0,Math.min(table.rows-1,row)); root.node.selectRow(row); table.positionViewAtRow(row,TableView.Contain); event.accepted=true
    }
    Rectangle {
        id: footer; anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
        height: root.viewState.treeNavigation ? 0 : 30; visible: !root.viewState.treeNavigation; color: (appTheme.palette && appTheme.color("secondary"))
        Rectangle { anchors.top: parent.top; width: parent.width; height: 1; visible: !root.viewState.treeNavigation; color: (appTheme.palette && appTheme.color("border")) }
        Text {
            anchors.left: parent.left; anchors.leftMargin: 9; anchors.verticalCenter: parent.verticalCenter
            text: table.rows.toLocaleString(Qt.locale(), "f", 0) + appLanguage.text("개")
            color: (appTheme.palette && appTheme.color("muted")); font.family: appTheme.family; font.pixelSize: 12
        }
        UiButton {
            objectName: "cell-details-button"
            visible: !root.viewState.rowDetails && !root.viewState.inline
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            text: appLanguage.text("셀 상세"); enabled: root.viewState.selected >= 0
            implicitHeight: 24; implicitWidth: 66; padding: 3
            font.family: appTheme.family; font.pixelSize: 12
            onClicked: root.showDetails()
        }
    }
    ColumnLayout {
        objectName: "table-empty-state"
        anchors.centerIn: root.frozen ? root : table; visible: table.rows === 0
        width: Math.max(0, Math.min(480, (root.frozen ? root.width : table.width) - 32)); spacing: 12
        Text {
            objectName: "table-empty-title"
            Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
            text: appLanguage.text(root.viewState.emptyText || "기록 없음")
            color: appTheme.palette.muted; font.family: appTheme.family; font.pixelSize: 14
        }
        Text {
            Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
            visible: text.length > 0; text: appLanguage.text(root.viewState.emptyDescription || "")
            color: appTheme.palette.muted; font.family: appTheme.family; font.pixelSize: 13
        }
        RowLayout {
            Layout.alignment: Qt.AlignHCenter; spacing: 8
            Repeater {
                model: root.node.nodes
                NodeChild { required property var modelData; node: modelData; horizontalParent: true }
            }
        }
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
        function onGeometryChanged() { table.forceLayout(); if (root.frozen) firstColumn.forceLayout() }
        function onScrollRequested(row,column) { table.forceLayout(); if (root.frozen && column===0) table.positionViewAtRow(row,TableView.Contain); else table.positionViewAtCell(Qt.point(column,row),TableView.Contain) }
    }
    Connections { target: root.node.verticalPosition; function onChanged() { table.contentY = root.node.verticalPosition.position } }
    Connections { target: root.node.horizontalPosition; function onChanged() { table.contentX = root.node.horizontalPosition.position } }
}
