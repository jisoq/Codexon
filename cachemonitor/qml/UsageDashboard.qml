import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var node
    readonly property var s: node.state
    readonly property var loading: s.analysisLoading || ({})
    readonly property bool masked: !!loading.active && !loading.retained
    readonly property var o: s.options
    readonly property bool tableView: o.view==='table'
    property real now: Date.now()/1000
    Timer {interval:60000;repeat:true;running:root.visible && root.o.selected.length>0;onTriggered:root.now=Date.now()/1000}
    function t(text) { return appLanguage.text(text) }
    function selected(items,value) { for(let i=0;i<items.length;i++) if(items[i].value===value)return i;return 0 }
    component Copy: Text {
        id: copyText
        property string tip: ""
        color: appTheme.palette.ink; font.family: appTheme.family; font.pixelSize: 14
        wrapMode: Text.Wrap; verticalAlignment: Text.AlignVCenter
        HoverHandler { id: hover }
        UiToolTip { visible: hover.hovered && copyText.tip.length>0; text: copyText.tip }
    }
    component Field: ColumnLayout {
        property string caption
        property var entries
        property string value
        property string field
        property bool compact: false
        spacing: compact?0:6
        Copy { visible:!compact; text: caption; font.pixelSize: 12; color: appTheme.palette.muted }
        UiChoice {
            objectName: "usage-"+field
            Layout.fillWidth:true; model: entries; textRole: "text"
            currentIndex: root.selected(entries,value); Accessible.name: caption
            onActivated: root.node.change(field,model[currentIndex].value)
        }
    }
    ColumnLayout {
        id: compare
        anchors.fill:parent;spacing:8
        RowLayout {
            Layout.fillWidth:true;spacing:10
            Field {compact:true;caption:root.t('조회 기간');field:'period';value:root.s.dateDraft.period;Layout.preferredWidth:130;Layout.maximumWidth:130;entries:[{text:root.t('전체 기록'),value:'all'},{text:root.t('오늘'),value:'today'},{text:root.t('최근 7일'),value:'7d'},{text:root.t('최근 30일'),value:'30d'},{text:root.t('직접 지정'),value:'custom'}]}
            Field {compact:true;caption:root.t('대분류');field:'dimension';value:root.o.dimension;entries:root.s.dimensions;Layout.preferredWidth:155;Layout.maximumWidth:155}
            Field {compact:true;caption:root.t(root.tableView?'정렬 지표':'차트 지표');field:'metric';value:root.o.metric;entries:root.s.metrics;Layout.preferredWidth:185;Layout.maximumWidth:185}
            UiButton {text:root.t('조회 조건');onClicked:root.node.change('filtersOpen','')}
            Item {Layout.fillWidth:true}
            UiButton {text:root.t('새로고침');role:'quiet';onClicked:root.node.change('refresh','')}
        }
        RowLayout {
            visible:root.s.dateDraft.period==='custom';Layout.fillWidth:true;spacing:12
            UiInput {objectName:'usage-date-start';Layout.preferredWidth:118;Layout.maximumWidth:118;placeholderText:'YYYY-MM-DD';text:root.s.dateDraft.start;Accessible.name:root.t('시작일');onTextEdited:root.node.change('start',text)}
            UiInput {objectName:'usage-date-end';Layout.preferredWidth:118;Layout.maximumWidth:118;placeholderText:'YYYY-MM-DD';text:root.s.dateDraft.end;Accessible.name:root.t('종료일');onTextEdited:root.node.change('end',text)}
            UiButton {objectName:'usage-date-apply';text:root.t('적용');enabled:root.s.dateCanApply;onClicked:root.node.change('applyDates','')}
            Copy {objectName:'usage-date-notice';Layout.fillWidth:true;text:root.s.dateNotice;color:root.s.dateError?appTheme.palette.error:appTheme.palette.muted;font.pixelSize:12;Accessible.name:text}
        }
        Flow {
            Layout.fillWidth:true;Layout.preferredHeight:childrenRect.height;spacing:12;visible:root.o.filtersOpen
            Repeater {model:root.s.choices||[];Field {required property var modelData;width:160;caption:modelData.title;entries:modelData.items;field:'filter:'+modelData.key;value:root.o.filters[modelData.key]||''}}
            UiCheckBox {text:root.t('보관 세션 포함');checked:root.o.archived;onToggled:root.node.change('archived',checked?'true':'false')}
        }
        RowLayout {
            visible:root.o.selected.length>0;Layout.fillWidth:true
            UiButton {text:'← '+root.t('뒤로');onClicked:root.node.back()}
            UiButton {text:root.t('지표 비교');role:'quiet';onClicked:root.node.reset()}
            Copy {text:root.masked?'':root.s.selectedLabel||'';Layout.fillWidth:true;elide:Text.ElideRight;wrapMode:Text.NoWrap;color:appTheme.palette.muted}
            UiButton {objectName:'usage-records';text:root.t('세션 기록 열기');enabled:!root.masked;onClicked:root.node.records()}
        }
        RowLayout {
            objectName:'usage-result-controls'
            visible:!root.masked;enabled:!root.masked
            Layout.fillWidth:true;spacing:10
            RowLayout {
                objectName:'usage-legend-row'
                UiChoice {objectName:'usage-view';Layout.preferredWidth:115;model:[root.t('차트'),root.t('주요 지표')];currentIndex:root.tableView?1:0;Accessible.name:root.t('보기');onActivated:root.node.change('view',currentIndex===1?'table':'chart')}
                UiChoice {visible:!root.tableView && !root.s.meanMetric;Layout.preferredWidth:95;model:[root.t('총량'),root.t('구성비')];currentIndex:root.o.mode==='ratio'?1:0;Accessible.name:root.t('표시 방식');onActivated:root.node.change('mode',currentIndex===1?'ratio':'amount')}
            }
            Item {Layout.fillWidth:true}
            UiButton {
                objectName:'usage-show-unknown';text:root.t('미확인 표시')
                checkable:true;checked:root.o.showUnknown===true
                onClicked:root.node.change('showUnknown',checked?'true':'false')
                ToolTip.visible:hovered
                ToolTip.text:root.t('모델, 추론 강도 또는 분류가 미확인인 항목을 표시합니다. 전체 합계는 유지됩니다.')
            }
            UiInput {objectName:'usage-search';Layout.preferredWidth:165;placeholderText:root.t('항목 검색');text:root.o.search;Accessible.name:root.t('항목 검색');onTextEdited:root.node.change('search',text)}
            UiChoice {Layout.preferredWidth:120;model:[root.t('큰 값부터'),root.t('작은 값부터'),root.t('이름순')];currentIndex:['desc','asc','name'].indexOf(root.o.sort);Accessible.name:root.t('항목 정렬');onActivated:root.node.change('sort',['desc','asc','name'][currentIndex])}
        }
        Copy {
            objectName:'usage-box-legend'
            visible:!root.masked && !root.tableView && !!root.s.meanMetric
            Layout.fillWidth:true;text:root.s.boxLegend||'';tip:root.s.boxTip||''
            font.pixelSize:12;color:appTheme.palette.muted;wrapMode:Text.NoWrap;elide:Text.ElideRight
            Accessible.name:text;Accessible.description:tip
        }
        Item {
            id: chart
            objectName:'usage-chart'
            Layout.fillWidth:true;Layout.fillHeight:true;Layout.minimumHeight:148
            Item {
                objectName:'usage-chart-content'
                visible:!root.masked;enabled:!root.masked
                anchors.fill:parent
                Item {
                    id: viewport
                    objectName:'usage-viewport'
                    anchors.fill:parent;clip:true
                    readonly property real tableWidth:width-rowsScroll.implicitWidth
                    readonly property real gap:8
                    readonly property real chevronWidth:28
                    readonly property bool stackedLabel:tableWidth<1000
                    readonly property real labelWidth:stackedLabel?0:210
                    readonly property var columns: {
                        const metrics=root.s.metrics
                        const weights=metrics.map(metric=>metric.value==='cost'||metric.value==='cost_per_call'?1.35:1)
                        const total=weights.reduce((sum,weight)=>sum+weight,0)
                        const start=labelWidth>0?labelWidth+12:0
                        const available=Math.max(0,tableWidth-start-chevronWidth-gap*(metrics.length-1))
                        let x=start
                        return weights.map(weight=>{
                            const column={x:x,width:available*weight/total}
                            x+=column.width+gap
                            return column
                        })
                    }
                    Item {
                        anchors.fill:parent
                        RowLayout {
                            id: chartHeader
                            visible:!root.tableView
                            width:parent.width;height:40;spacing:16
                            Copy {text:root.t('모델 / 추론 강도');Layout.preferredWidth:180;font.pixelSize:12;color:appTheme.palette.muted}
                            Item {
                                Layout.fillWidth:true;Layout.preferredHeight:20
                                Copy {x:root.s.meanMetric?5:0;text:'0';font.pixelSize:12;color:appTheme.palette.muted}
                                Copy {anchors.horizontalCenter:parent.horizontalCenter;text:root.s.middle||'';font.pixelSize:12;color:appTheme.palette.muted}
                                Copy {anchors.right:parent.right;anchors.rightMargin:root.s.meanMetric?5:0;text:root.s.upper||'';font.pixelSize:12;color:appTheme.palette.muted}
                            }
                            Copy {text:root.s.metricLabel||'';Layout.preferredWidth:110;font.pixelSize:12;horizontalAlignment:Text.AlignRight;color:appTheme.palette.muted;tip:root.o.metric==='tps'?root.s.tpsTip:''}
                            Copy {text:root.s.meanMetric?root.t('유효 호출 / 전체'):root.s.shareLabel||'';Layout.preferredWidth:90;font.pixelSize:12;horizontalAlignment:Text.AlignRight;color:appTheme.palette.muted}
                            Item {Layout.preferredWidth:20}
                        }
                        Item {
                            id:tableHeader
                            objectName:'usage-table-header'
                            visible:root.tableView;width:viewport.tableWidth
                            height:Math.max(40,headerValues.y+headerValues.height+8)
                            Copy {
                                objectName:'usage-table-label-header'
                                text:root.t('모델 / 추론 강도');font.pixelSize:12;color:appTheme.palette.muted
                                width:viewport.stackedLabel?parent.width:viewport.labelWidth
                                height:viewport.stackedLabel?24:parent.height
                                elide:Text.ElideRight;wrapMode:Text.NoWrap
                            }
                            Item {
                                id:headerValues
                                y:viewport.stackedLabel?28:8
                                width:parent.width;height:childrenRect.height
                                Repeater {
                                    model:root.s.metrics
                                    Copy {
                                        required property var modelData
                                        required property int index
                                        objectName:'usage-header-'+modelData.value
                                        x:viewport.columns[index].x;width:viewport.columns[index].width;height:implicitHeight
                                        text:modelData.text;font.pixelSize:12;color:appTheme.palette.muted
                                        horizontalAlignment:Text.AlignRight
                                    }
                                }
                            }
                        }
                        Rectangle {y:root.tableView?tableHeader.height:40;width:parent.width;height:1;color:appTheme.palette.border}
                        ListView {
                            id: rowsView
                            objectName:'usage-rows'
                            anchors.fill:parent;anchors.topMargin:(root.tableView?tableHeader.height:40)+1
                            model:root.node.rowModel
                            clip:true;reuseItems:true;cacheBuffer:108
                            boundsBehavior:Flickable.StopAtBounds;flickableDirection:Flickable.VerticalFlick
                            ScrollBar.vertical:UiScrollBar {id:rowsScroll}
                            delegate: Column {
                                id:groupLine
                                required property var modelData
                                width:root.tableView?viewport.tableWidth:ListView.view.width
                                Copy {visible:groupLine.modelData.groupStart;width:parent.width;height:28;text:groupLine.modelData.group;font.pixelSize:13;font.bold:true;elide:Text.ElideRight;wrapMode:Text.NoWrap;tip:groupLine.modelData.group}
                                AbstractButton {
                                    id:line
                                    objectName:'usage-row-'+modelData.key
                                    readonly property var modelData:groupLine.modelData
                                    width:parent.width
                                    height:root.tableView?Math.max(54,tableValues.height+(viewport.stackedLabel?tableLabel.implicitHeight+24:16)):54
                                    hoverEnabled:true;activeFocusOnTab:true
                                    Accessible.name:modelData.group+' / '+modelData.label+' '+modelData.value
                                    Accessible.description:modelData.tip+'\n'+modelData.coverage
                                    onClicked:root.node.select(modelData.key)
                                    background:Rectangle {color:line.hovered||line.activeFocus?appTheme.palette.secondary:'transparent';Rectangle {anchors.bottom:parent.bottom;width:parent.width;height:1;color:appTheme.palette.border;opacity:.4}}
                                    contentItem:Item {
                                        RowLayout {
                                            visible:!root.tableView;anchors.fill:parent;spacing:16
                                            Copy {text:line.modelData.label;Layout.preferredWidth:180;elide:Text.ElideRight;wrapMode:Text.NoWrap;tip:line.modelData.label}
                                            Item {
                                                id:plotCell
                                                objectName:'usage-plot-'+line.modelData.key
                                                readonly property var plot:line.modelData.plot||({})
                                                readonly property real span:Math.max(0,width-10)
                                                readonly property color markColor:appTheme.color(line.modelData.color)
                                                function position(value) {return 5+span*value}
                                                Layout.fillWidth:true;Layout.preferredHeight:24
                                                Rectangle {
                                                    objectName:'usage-total-bar'
                                                    visible:!root.s.meanMetric
                                                    height:22;width:parent.width*line.modelData.fraction;anchors.verticalCenter:parent.verticalCenter;radius:3;color:plotCell.markColor
                                                }
                                                Item {
                                                    objectName:'usage-box-marks'
                                                    anchors.fill:parent;visible:plotCell.plot.kind==='box'
                                                    Rectangle {objectName:'usage-whisker';x:plotCell.position(plotCell.plot.p10||0);y:11;width:Math.max(1,plotCell.span*((plotCell.plot.p90||0)-(plotCell.plot.p10||0)));height:2;color:plotCell.markColor}
                                                    Rectangle {x:plotCell.position(plotCell.plot.p10||0)-1;y:7;width:2;height:10;color:plotCell.markColor}
                                                    Rectangle {x:plotCell.position(plotCell.plot.p90||0)-1;y:7;width:2;height:10;color:plotCell.markColor}
                                                    Rectangle {
                                                        objectName:'usage-box'
                                                        x:plotCell.position(plotCell.plot.q1||0);y:5
                                                        width:Math.max(1,plotCell.span*((plotCell.plot.q3||0)-(plotCell.plot.q1||0)));height:14
                                                        color:Qt.alpha(plotCell.markColor,.23);border.color:plotCell.markColor
                                                    }
                                                    Rectangle {objectName:'usage-median';x:plotCell.position(plotCell.plot.median||0)-1;y:3;width:2;height:18;color:appTheme.palette.ink}
                                                    Rectangle {objectName:'usage-mean';x:plotCell.position(plotCell.plot.mean||0)-5;y:7;width:10;height:10;radius:5;color:plotCell.markColor;border.width:2;border.color:appTheme.palette.ink}
                                                }
                                                HoverHandler {id:plotHover}
                                                UiToolTip {visible:plotHover.hovered && line.modelData.tip.length>0;text:line.modelData.tip}
                                                Copy {visible:line.modelData.missing;text:root.t('측정 불가');font.pixelSize:12;color:appTheme.palette.muted}
                                            }
                                            Copy {text:line.modelData.value+(line.modelData.partial?' *':'');Layout.preferredWidth:110;horizontalAlignment:Text.AlignRight;tip:line.modelData.tip+'\n'+line.modelData.coverage}
                                            Copy {text:root.s.meanMetric?line.modelData.coverage:line.modelData.share;Layout.preferredWidth:90;horizontalAlignment:Text.AlignRight;font.pixelSize:12;color:appTheme.palette.muted;tip:line.modelData.coverage}
                                            Copy {text:'›';Layout.preferredWidth:20;font.pixelSize:20}
                                        }
                                        Item {
                                            visible:root.tableView;anchors.fill:parent
                                            Copy {
                                                id:tableLabel
                                                objectName:'usage-table-label'
                                                text:line.modelData.label
                                                width:viewport.stackedLabel?parent.width-viewport.chevronWidth:viewport.labelWidth
                                                height:implicitHeight;y:viewport.stackedLabel?8:(parent.height-height)/2
                                                elide:Text.ElideRight;wrapMode:Text.NoWrap;tip:line.modelData.label
                                            }
                                            Item {
                                                id:tableValues
                                                y:viewport.stackedLabel?tableLabel.height+16:(parent.height-height)/2
                                                width:parent.width;height:childrenRect.height
                                                Repeater {
                                                    model:line.modelData.metrics||[]
                                                    Copy {
                                                        required property var modelData
                                                        required property int index
                                                        objectName:'usage-cell-'+modelData.key
                                                        x:viewport.columns[index].x;width:viewport.columns[index].width;height:implicitHeight
                                                        text:modelData.value+(modelData.partial?' *':'')
                                                        horizontalAlignment:Text.AlignRight;font.bold:modelData.key===root.o.metric
                                                        tip:root.t('유효 호출 / 전체')+': '+modelData.tip
                                                    }
                                                }
                                            }
                                            Copy {objectName:'usage-table-chevron';text:'›';x:parent.width-20;width:20;height:parent.height;font.pixelSize:20}
                                        }
                                    }
                                }
                            }
                        }
                        Copy {visible:rowsView.count===0;y:(root.tableView?tableHeader.height:40)+1;width:parent.width;height:100;text:root.t('조건에 맞는 기록 없음');horizontalAlignment:Text.AlignHCenter;color:appTheme.palette.muted}
                    }
                }
            }
            Loader { anchors.fill:parent;active:root.masked;sourceComponent:Component { AnalysisLoading { status:root.loading } } }
        }
        RowLayout {
            Layout.fillWidth:true
            Copy {text:root.masked?'':root.s.scope;Layout.fillWidth:true;font.pixelSize:12;color:appTheme.palette.muted;elide:Text.ElideRight;wrapMode:Text.NoWrap}
            Copy {text:root.masked?'':root.s.countLabel;font.pixelSize:12;color:appTheme.palette.muted}
        }
    }
}
