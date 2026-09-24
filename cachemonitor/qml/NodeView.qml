import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import CacheMonitor 6.0

Item {
    id: view
    property alias item: content.item
    property alias sourceComponent: content.sourceComponent
    Loader { id: content; anchors.fill: parent }
    required property var node
    property var s: node ? node.state : ({})
    property bool horizontalParent: false
    activeFocusOnTab: s.kind === "plot"
    onActiveFocusChanged: if (activeFocus && s.kind === "plot" && item) item.forceActiveFocus()
    visible: !!node && s.visible
    enabled: !!node && s.enabled
    objectName: node ? node.objectName : ""
    implicitWidth: Math.max(s.minWidth || 0, s.width >= 0 ? s.width : (item ? item.implicitWidth : 0))
    implicitHeight: Math.max(s.minHeight || 0, s.height >= 0 ? s.height : (item ? item.implicitHeight : 0))
    Layout.alignment: s.kind === "navigation" ? Qt.AlignTop : 0
    Layout.minimumWidth: s.minWidth || 0
    Layout.minimumHeight: s.minHeight || 0
    Layout.maximumWidth: s.maxWidth || Infinity
    Layout.maximumHeight: s.maxHeight || Infinity
    Layout.preferredWidth: s.width >= 0 ? s.width : implicitWidth
    Layout.preferredHeight: s.height >= 0 ? s.height : implicitHeight
    Layout.fillWidth: s.width < 0 && (!horizontalParent || s.stretch > 0 || !!s.expandX)
    Layout.fillHeight: s.height < 0 && (s.stretch > 0 || !!s.expandY || (horizontalParent && ["group","row","column","scroll","plot"].includes(s.kind)) || ["stack", "tabs", "split"].includes(s.kind))
    sourceComponent: {
        if (!node) return spacerComponent
        switch(s.kind) {
        case "row": return s.flow ? flowComponent : s.minColumnWidth ? adaptiveGridComponent : s.collapseBelow ? responsiveComponent : rowComponent
        case "column": return columnComponent
        case "group": return groupComponent
        case "spacer": return spacerComponent
        case "text": return textComponent
        case "textarea": return textAreaComponent
        case "button": return buttonComponent
        case "toggle": return toggleComponent
        case "switch": return switchComponent
        case "choice": return choiceComponent
        case "navigation": return navigationComponent
        case "input": return inputComponent
        case "date": return dateComponent
        case "slider": return sliderComponent
        case "scroll": return scrollComponent
        case "stack": return stackComponent
        case "tabs": return tabsComponent
        case "split": return splitComponent
        case "table": return tableComponent
        case "plot": return plotComponent
        default: return spacerComponent
        }
    }
    Component { id: spacerComponent; Item {} }
    Component {
        id: groupComponent
        Rectangle {
            color: appTheme.palette && appTheme.color(view.s.background || "transparent")
            radius: view.s.radius || 0
            border.color: appTheme.palette && appTheme.color(view.s.border || "transparent")
            border.width: view.s.border ? 1 : 0
            implicitWidth: childrenView.implicitWidth
            implicitHeight: childrenView.implicitHeight
            Rectangle {
                anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                height: 1; color: (appTheme.palette && appTheme.color("border")); visible: view.s.style === "preferenceRow"
            }
            NodeChild {
                id: childrenView; anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter
                width: Math.min(parent.width, view.s.contentWidth || parent.width)
                node: view.node && view.node.nodes.length ? view.node.nodes[0] : null
            }
        }
    }
    Component {
        id: flowComponent
        Flow {
            layoutDirection: view.s.alignRight ? Qt.RightToLeft : Qt.LeftToRight
            spacing: view.s.spacing
            Repeater {
                model: view.node.nodes
                NodeChild {
                    required property var modelData
                    node: modelData
                    visible: modelData.state.visible && modelData.state.kind !== "spacer"
                    width: Math.max(modelData.state.minWidth, implicitWidth)
                    height: Math.max(36, implicitHeight)
                }
            }
        }
    }
    Component {
        id: adaptiveGridComponent
        Item {
            implicitHeight: grid.implicitHeight + view.s.margins[1] + view.s.margins[3]
            GridLayout {
                id: grid
                anchors.fill: parent
                anchors.leftMargin: view.s.margins[0]; anchors.rightMargin: view.s.margins[2]
                anchors.topMargin: view.s.margins[1]; anchors.bottomMargin: view.s.margins[3]
                columns: Math.max(1, Math.min(view.node.nodes.length, Math.floor((width + columnSpacing) / (view.s.minColumnWidth + columnSpacing))))
                columnSpacing: view.s.spacing; rowSpacing: view.s.spacing
                Repeater {
                    model: view.node.nodes
                    NodeChild {
                        required property var modelData
                        node: modelData
                        Layout.fillWidth: true; Layout.fillHeight: false; Layout.alignment: Qt.AlignTop
                        Layout.minimumWidth: 0; Layout.preferredWidth: 1
                    }
                }
            }
        }
    }
    Component {
        id: responsiveComponent
        GridLayout {
            columns: view.width < view.s.collapseBelow ? 1 : (view.s.columns || 2)
            columnSpacing: view.s.spacing; rowSpacing: view.s.spacing
            Repeater { model: view.node.nodes; NodeChild { required property var modelData; node: modelData; Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.preferredWidth: (modelData.state.stretch || 1) * 100 } }
        }
    }
    Component {
        id: rowComponent
        Item {
            implicitWidth: row.implicitWidth + view.s.margins[0] + view.s.margins[2]
            implicitHeight: row.implicitHeight + view.s.margins[1] + view.s.margins[3]
            RowLayout {
                id: row; anchors.fill: parent
                anchors.leftMargin: view.s.margins[0]; anchors.topMargin: view.s.margins[1]
                anchors.rightMargin: view.s.margins[2]; anchors.bottomMargin: view.s.margins[3]
                spacing: view.s.spacing
                Repeater { model: view.node.nodes; NodeChild { required property var modelData; node: modelData; horizontalParent: true } }
            }
        }
    }
    Component {
        id: columnComponent
        Item {
            implicitWidth: column.implicitWidth + view.s.margins[0] + view.s.margins[2]
            implicitHeight: column.implicitHeight + view.s.margins[1] + view.s.margins[3]
            ColumnLayout {
                id: column; anchors.fill: parent
                anchors.leftMargin: view.s.margins[0]; anchors.topMargin: view.s.margins[1]
                anchors.rightMargin: view.s.margins[2]; anchors.bottomMargin: view.s.margins[3]
                spacing: view.s.spacing
                Repeater { model: view.node.nodes; NodeChild { required property var modelData; node: modelData } }
            }
        }
    }
    Component {
        id: textComponent
        Item {
            implicitWidth: (view.s.wrap ? 220 : Math.min(900, label.implicitWidth)) + view.s.margins[0] + view.s.margins[2] + (view.s.swatch ? 18 : 0)
            // Text and TextEdit use different line metrics. Reserve the height
            // of the visible renderer, including its final wrapped line.
            implicitHeight: Math.ceil(view.s.selectable ? selectableText.contentHeight : label.contentHeight) + view.s.margins[1] + view.s.margins[3]
            Text {
                id: label; anchors.fill: parent
                anchors.leftMargin: view.s.margins[0] + (view.s.swatch ? 18 : 0); anchors.rightMargin: view.s.margins[2]
                anchors.topMargin: view.s.margins[1]; anchors.bottomMargin: view.s.margins[3]
                visible: !view.s.selectable
                text: view.s.text; textFormat: view.s.rich || view.s.externalLinks ? Text.RichText : Text.PlainText
                wrapMode: view.s.wrap ? Text.Wrap : Text.NoWrap
                elide: view.s.wrap || view.s.noElide ? Text.ElideNone : Text.ElideRight
                font.family: view.s.fontFamily || appTheme.family; font.pixelSize: view.s.fontSize; font.bold: view.s.bold
                color: appTheme.palette && appTheme.color(view.s.color)
                horizontalAlignment: view.s.alignment & Qt.AlignRight ? Text.AlignRight : Text.AlignLeft
                linkColor: (appTheme.palette && appTheme.color("accent"))
                verticalAlignment: Text.AlignVCenter
                Accessible.name: view.s.accessible || text
                onLinkActivated: link => { if (view.s.externalLinks) Qt.openUrlExternally(link) }
            }
            TextEdit {
                id: selectableText
                anchors.fill: label; visible: !!view.s.selectable
                text: label.text; textFormat: view.s.rich || view.s.externalLinks ? TextEdit.RichText : TextEdit.PlainText
                wrapMode: view.s.wrap ? TextEdit.Wrap : TextEdit.NoWrap
                font: label.font; color: label.color; readOnly: true; selectByMouse: true; activeFocusOnTab: true
                selectionColor: appTheme.palette.selection; selectedTextColor: appTheme.palette.ink
                onLinkActivated: link => { if (view.s.externalLinks) Qt.openUrlExternally(link) }
            }
            Rectangle {
                visible: !!view.s.swatch
                anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                width: 12; height: 4
                color: view.s.swatch ? (appTheme.palette && appTheme.color(view.s.swatch)) : "transparent"
            }
            HoverHandler { id: textHover }
            UiToolTip { visible: textHover.hovered && (view.s.tooltip.length > 0 || label.truncated); text: view.s.tooltip || label.text }
        }
    }
    Component {
        id: textAreaComponent
        ScrollView {
            implicitHeight: 170; implicitWidth: 200; clip: true
            contentWidth: availableWidth
            TextArea {
                text: view.s.text; readOnly: true; selectByMouse: true
                textFormat: view.s.rich ? TextEdit.RichText : TextEdit.PlainText
                wrapMode: TextEdit.Wrap; font.family: appTheme.family; font.pixelSize: 14
                placeholderText: view.s.placeholder; color: (appTheme.palette && appTheme.readableText(view.s.color, "secondary"))
                selectionColor: appTheme.palette.accent
                selectedTextColor: appTheme.readableText("surface", "accent")
                background: Rectangle { color: (appTheme.palette && appTheme.color("secondary")); border.color: (appTheme.palette && appTheme.color("border")) }
            }
        }
    }
    Component {
        id: buttonComponent
        UiButton {
            text: view.s.text; checkable: view.s.checkable; checked: view.s.checked
            font.pixelSize: view.s.fontSize; font.bold: view.s.bold
            disclosure: !!view.s.disclosure
            iconName: view.s.iconName || ""
            selectionTab: !!view.s.selectionTab
            flat: !!view.s.flat
            Component.onCompleted: if (view.s.defaultFocus) forceActiveFocus()
            onClicked: view.node.activate()
            Accessible.name: view.s.accessible || text
            UiToolTip { visible: parent.hovered && view.s.tooltip.length > 0; text: view.s.tooltip }
        }
    }
    Component {
        id: toggleComponent
        UiCheckBox {
            text: view.s.text; checked: view.s.checked; onClicked: view.node.activate()
            Accessible.name: view.s.accessible || text
            UiToolTip { visible: parent.hovered && view.s.tooltip.length > 0; text: view.s.tooltip }
        }
    }
    Component {
        id: switchComponent
        UiSwitch {
            checked: view.s.checked; onClicked: view.node.activate()
            Accessible.name: view.s.accessible
            UiToolTip { visible: parent.hovered && view.s.tooltip.length > 0; text: view.s.tooltip }
        }
    }
    Component {
        id: choiceComponent
        UiChoice {
            model: view.s.items; textRole: "text"; currentIndex: view.s.index
            wheelEnabled: true
            implicitWidth: 180
            onActivated: index => view.node.choose(index)
            Accessible.name: view.s.accessible
            tooltipText: view.s.tooltip
        }
    }
    Component {
        id: navigationComponent
        ListView {
            implicitWidth: 166; implicitHeight: contentHeight; clip: true
            spacing: 4
            model: view.s.items; currentIndex: view.s.index; keyNavigationEnabled: true
            onCurrentIndexChanged: if (activeFocus && currentIndex !== view.s.index) view.node.choose(currentIndex)
            delegate: ItemDelegate {
                id: navigationItem
                hoverEnabled:true
                HoverHandler { cursorShape: Qt.PointingHandCursor }
                required property var modelData; required property int index
                width: ListView.view.width; text: modelData.text; highlighted: index === view.s.index
                implicitHeight: 42
                leftPadding: 14; rightPadding: 12
                contentItem: Text {
                    text: modelData.text; font.family: appTheme.family; font.pixelSize: 14
                    font.weight: highlighted ? Font.DemiBold : Font.Normal
                    color: highlighted ? (appTheme.palette && appTheme.color("accent")) : (appTheme.palette && appTheme.color("muted"))
                    verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
                }
                background: Rectangle {
                    radius: 6; color: highlighted ? (appTheme.palette && appTheme.color("secondary")) : parent.hovered ? (appTheme.palette && appTheme.color("secondary")) : "transparent"
                    border.color: parent.activeFocus ? (appTheme.palette && appTheme.color("accent")) : "transparent"
                    Rectangle { objectName:"hover-feedback";anchors.fill:parent;radius:6;color:appTheme.palette.accent;opacity:navigationItem.hovered ? .08 : 0 }
                }
                onClicked: view.node.choose(index)
            }
        }
    }
    Component {
        id: inputComponent
        UiInput {
            text: view.s.text; placeholderText: view.s.placeholder
            Accessible.name: view.s.accessible || placeholderText
            onTextEdited: view.node.edit(text)
        }
    }
    Component { id: dateComponent; DateField { node: view.node } }
    Component {
        id: sliderComponent
        UiSlider {
            from: view.s.minimum; to: view.s.maximum; stepSize: 1; value: view.s.value
            onMoved: view.node.slide(Math.round(value))
            Accessible.name: view.s.accessible
        }
    }
    Component {
        id: scrollComponent
        ScrollView {
            id: scroll; clip: true; implicitHeight: 240; implicitWidth: 240
            padding: 0; rightPadding: 12
            property var revealTarget: null
            property string revealGeometry: ""
            function cancelReveal() { revealTarget=null; revealGeometry="" }
            onVisibleChanged: if (!visible) cancelReveal()
            Keys.onPressed: event => {
                if ([Qt.Key_Up,Qt.Key_Down,Qt.Key_Left,Qt.Key_Right,Qt.Key_PageUp,Qt.Key_PageDown,Qt.Key_Home,Qt.Key_End].includes(event.key)) cancelReveal()
                event.accepted=false
            }
            WheelHandler {
                target: null
                onWheel: event => {
                    scroll.cancelReveal()
                    let horizontal=event.angleDelta.x !== 0 || (event.modifiers & Qt.ShiftModifier)
                    let pixels=horizontal ? event.pixelDelta.x:event.pixelDelta.y
                    let angle=horizontal ? (event.angleDelta.x || event.angleDelta.y):event.angleDelta.y
                    let delta=pixels || angle/120*60
                    if (horizontal) scroll.contentItem.contentX=Math.max(0,Math.min(scroll.contentWidth-scroll.availableWidth,scroll.contentItem.contentX-delta))
                    else scroll.contentItem.contentY=Math.max(0,Math.min(scroll.contentHeight-scroll.availableHeight,scroll.contentItem.contentY-delta))
                    event.accepted=true
                }
            }
            ScrollBar.horizontal: UiScrollBar {
                orientation: Qt.Horizontal
                policy: view.s.horizontalScroll === false ? ScrollBar.AlwaysOff : ScrollBar.AsNeeded
                onPressedChanged: if (pressed) scroll.cancelReveal()
            }
            ScrollBar.vertical: UiScrollBar {
                parent: scroll
                x: scroll.width - width; y: 0; height: scroll.availableHeight
                orientation: Qt.Vertical
                policy: view.s.verticalScroll === false ? ScrollBar.AlwaysOff : ScrollBar.AsNeeded
                active: true
                onPressedChanged: if (pressed) scroll.cancelReveal()
            }
            contentWidth: body.width; contentHeight: body.implicitHeight
            Connections {
                target: scroll.contentItem
                function onContentYChanged() { view.node.verticalPosition.observe(scroll.contentItem.contentY) }
                function onMovementStarted() { scroll.cancelReveal() }
            }
            Connections {
                target: view.node.verticalPosition
                function onRequested(value) { scroll.cancelReveal(); scroll.contentItem.contentY=value }
                function onChanged() { scroll.contentItem.contentY = view.node.verticalPosition.position }
            }
            NodeChild {
                id: body; node: view.node && view.node.nodes.length ? view.node.nodes[0] : null
                width: Math.max(scroll.availableWidth, node ? node.state.minWidth : 0)
                height: Math.max(implicitHeight, view.s.fillViewport ? scroll.availableHeight : 0)
            }
            function findNode(item, target) {
                if (item.node === target) return item
                for (let child of item.children) {
                    let result = findNode(child, target)
                    if (result) return result
                }
                return null
            }
            FrameAnimation {
                running: scroll.revealTarget !== null && scroll.visible
                onTriggered: {
                    let item=scroll.findNode(body,scroll.revealTarget)
                    if (!item || !item.visible) { scroll.cancelReveal(); return }
                    if (scroll.availableHeight <= 0 || scroll.availableWidth <= 0) return
                    let point=item.mapToItem(body,0,0)
                    let geometry=[point.y,item.width,item.height,body.width,scroll.contentHeight,scroll.availableHeight].join("|")
                    // A complete frame with unchanged geometry lets nested text and
                    // layouts settle before measuring the requested section.
                    if (geometry !== scroll.revealGeometry) { scroll.revealGeometry=geometry; return }
                    scroll.contentItem.contentY=Math.max(0,Math.min(point.y,scroll.contentHeight-scroll.availableHeight))
                    scroll.cancelReveal()
                }
            }
            Connections {
                target: view.node
                function onRevealRequested(target) {
                    scroll.revealTarget=target
                    scroll.revealGeometry=""
                }
            }
        }
    }
    Component {
        id: stackComponent
        StackLayout {
            currentIndex: view.s.index
            Repeater { model: view.node.nodes; NodeChild { required property var modelData; node: modelData; Layout.fillWidth: true; Layout.fillHeight: true } }
        }
    }
    Component {
        id: tabsComponent
        Rectangle {
          color: (appTheme.palette && appTheme.color("surface")); radius: 6; border.color: (appTheme.palette && appTheme.color("border"))
          implicitWidth: tabsLayout.implicitWidth; implicitHeight: tabsLayout.implicitHeight
          ColumnLayout {
            id: tabsLayout; anchors.fill: parent; anchors.margins: 1; spacing: 0
            TabBar {
                Layout.fillWidth: true; currentIndex: view.s.index
                background: Rectangle { color: (appTheme.palette && appTheme.color("surface")); Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: (appTheme.palette && appTheme.color("border")) } }
                Repeater {
                    model: view.node.nodes
                    TabButton {
                        HoverHandler { cursorShape: Qt.PointingHandCursor }
                        required property var modelData; required property int index
                        text: modelData.state.tabTitle; implicitHeight: 44
                        width: implicitWidth; leftPadding: 18; rightPadding: 18
                        contentItem: Text {
                            text: modelData.state.tabTitle; font.family: appTheme.family; font.pixelSize: 14
                            font.bold: index === view.s.index
                            color: index === view.s.index ? (appTheme.palette && appTheme.color("accent")) : (appTheme.palette && appTheme.color("muted"))
                            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                            elide: Text.ElideRight
                        }
                        background: Rectangle {
                            color: parent.hovered ? (appTheme.palette && appTheme.color("secondary")) : "transparent"
                            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: index === view.s.index ? 2 : 1; color: index === view.s.index ? (appTheme.palette && appTheme.color("accent")) : (appTheme.palette && appTheme.color("border")) }
                        }
                        onClicked: view.node.choose(index)
                    }
                }
            }
            StackLayout {
                Layout.fillHeight: true; Layout.fillWidth: true; currentIndex: view.s.index
                Repeater { model: view.node.nodes; NodeChild { required property var modelData; node: modelData; Layout.fillWidth: true; Layout.fillHeight: true } }
            }
          }
        }
    }
    Component {
        id: splitComponent
        SplitView {
            id: splitter
            orientation: view.s.horizontal ? Qt.Horizontal : Qt.Vertical
            handle: Rectangle {
                implicitWidth: 10; implicitHeight: 10; color: "transparent"
                Rectangle { anchors.centerIn: parent; width: 2; height: 28; radius: 1; color: SplitHandle.hovered ? (appTheme.palette && appTheme.color("accent")) : (appTheme.palette && appTheme.color("border")) }
            }
            onWidthChanged: if (!resizing) view.node.requestFit()
            onResizingChanged: {
                if (!resizing) {
                    let sizes=[]
                    for (let i=0;i<panes.count;i++) sizes.push(panes.itemAt(i).width)
                    view.node.resized(sizes)
                }
            }
            Repeater {
                id: panes
                model: view.node.nodes
                NodeChild {
                    required property var modelData; required property int index
                    visible: modelData.state.visible && (view.s.focusIndex === undefined || view.s.focusIndex < 0 || view.s.focusIndex === index)
                    node: modelData; SplitView.minimumWidth: 180
                    SplitView.preferredWidth: view.s.focusIndex === index ? splitter.width : Math.max(180, (splitter.width - 12) * ((view.s.sizes[index] || 300) / view.s.sizes.reduce((a,b) => a+b, 0)))
                    SplitView.maximumWidth: view.s.focusIndex === index ? Infinity : modelData.state.maxWidth
                }
            }
        }
    }
    Component { id: tableComponent; DataTable { node: view.node } }
    Component {
        id: plotComponent
        QuickPlot {
            id: plot; source: view.node; implicitHeight: view.s.minHeight; implicitWidth: view.s.minWidth
            Accessible.name: view.s.accessible || appLanguage.text("차트 선택")
            Accessible.role: Accessible.Chart
            HoverHandler { id: plotHover; objectName: "plotHover"; cursorShape: plot.hoverIndex >= 0 ? Qt.PointingHandCursor : Qt.ArrowCursor; onPointChanged: plot.showTip(point.position.x, point.position.y); onHoveredChanged: if (!hovered) plot.clearHover() }
            UiToolTip { visible: !view.s.quotaDetail && plotHover.hovered && plot.tip.length > 0; text: plot.tip }
            Loader {
                id: quotaDetailLoader
                active: !!view.s.quotaDetail
                sourceComponent: Component { QuotaDetails { plot: quotaDetailLoader.parent } }
            }
            TapHandler { onTapped: eventPoint => { plot.forceActiveFocus(); plot.activateAt(eventPoint.position.x,eventPoint.position.y) } }
            Keys.onPressed: event => { plot.key(event.key); event.accepted = true }
        }
    }
}
