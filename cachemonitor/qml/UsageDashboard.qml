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
            UiButton {text:root.t('세션 기록 열기');enabled:!root.masked;onClicked:root.node.records()}
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
            UiInput {Layout.preferredWidth:165;placeholderText:root.t('항목 검색');text:root.o.search;Accessible.name:root.t('항목 검색');onTextEdited:root.node.change('search',text)}
            UiChoice {Layout.preferredWidth:120;model:[root.t('큰 값부터'),root.t('작은 값부터'),root.t('이름순')];currentIndex:['desc','asc','name'].indexOf(root.o.sort);Accessible.name:root.t('항목 정렬');onActivated:root.node.change('sort',['desc','asc','name'][currentIndex])}
        }
        Item {
            id: chart
            objectName:'usage-chart'
            Layout.fillWidth:true;Layout.fillHeight:true;Layout.minimumHeight:148
            Item {
                objectName:'usage-chart-content'
                visible:!root.masked;enabled:!root.masked
                anchors.fill:parent
                Flickable {
                    id: horizontal
                    objectName:'usage-horizontal'
                    anchors.fill:parent;clip:true
                    contentWidth:root.tableView?Math.max(width,1130):width
                    contentHeight:height
                    flickableDirection:Flickable.HorizontalFlick;boundsBehavior:Flickable.StopAtBounds
                    ScrollBar.horizontal:UiScrollBar {policy:root.tableView?ScrollBar.AsNeeded:ScrollBar.AlwaysOff}
                    Item {
                        width:horizontal.contentWidth;height:horizontal.height-(root.tableView?14:0)
                        RowLayout {
                            id: chartHeader
                            visible:!root.tableView
                            width:parent.width;height:40;spacing:16
                            Copy {text:root.t('모델 / 추론 강도');Layout.preferredWidth:180;font.pixelSize:12;color:appTheme.palette.muted}
                            RowLayout {Layout.fillWidth:true;Copy{text:'0';font.pixelSize:12;color:appTheme.palette.muted}Item{Layout.fillWidth:true}Copy{text:root.s.middle||'';font.pixelSize:12;color:appTheme.palette.muted}Item{Layout.fillWidth:true}Copy{text:root.s.upper||'';font.pixelSize:12;color:appTheme.palette.muted}}
                            Copy {text:root.s.metricLabel||'';Layout.preferredWidth:110;font.pixelSize:12;horizontalAlignment:Text.AlignRight;color:appTheme.palette.muted;tip:root.o.metric==='tps'?root.s.tpsTip:''}
                            Copy {text:root.s.meanMetric?root.t('유효 호출 / 전체'):root.s.shareLabel||'';Layout.preferredWidth:90;font.pixelSize:12;horizontalAlignment:Text.AlignRight;color:appTheme.palette.muted}
                            Item {Layout.preferredWidth:20}
                        }
                        RowLayout {
                            visible:root.tableView;width:parent.width;height:40;spacing:12
                            Copy {text:root.t('모델 / 추론 강도');Layout.preferredWidth:210;font.pixelSize:12;color:appTheme.palette.muted}
                            Repeater {model:root.s.tableMetrics;Copy {required property var modelData;text:modelData.text;Layout.fillWidth:true;Layout.preferredWidth:110;font.pixelSize:12;color:appTheme.palette.muted;horizontalAlignment:Text.AlignRight}}
                            Item {Layout.preferredWidth:20}
                        }
                        Rectangle {y:40;width:parent.width;height:1;color:appTheme.palette.border}
                        ListView {
                            id: rowsView
                            objectName:'usage-rows'
                            anchors.fill:parent;anchors.topMargin:41
                            model:root.node.rowModel
                            clip:true;reuseItems:true;cacheBuffer:108
                            boundsBehavior:Flickable.StopAtBounds
                            ScrollBar.vertical:UiScrollBar {}
                            delegate: Column {
                                id:groupLine
                                required property var modelData
                                width:ListView.view.width
                                Copy {visible:groupLine.modelData.groupStart;width:parent.width;height:28;text:groupLine.modelData.group;font.pixelSize:13;font.bold:true;elide:Text.ElideRight;wrapMode:Text.NoWrap;tip:groupLine.modelData.group}
                                AbstractButton {
                                    id:line
                                    objectName:'usage-row-'+modelData.key
                                    readonly property var modelData:groupLine.modelData
                                    width:parent.width;height:54;hoverEnabled:true;activeFocusOnTab:true
                                    Accessible.name:modelData.group+' / '+modelData.label+' '+modelData.value
                                    onClicked:root.node.select(modelData.key)
                                    background:Rectangle {color:line.hovered||line.activeFocus?appTheme.palette.secondary:'transparent';Rectangle {anchors.bottom:parent.bottom;width:parent.width;height:1;color:appTheme.palette.border;opacity:.4}}
                                    contentItem:Item {
                                        RowLayout {
                                            visible:!root.tableView;anchors.fill:parent;spacing:16
                                            Copy {text:line.modelData.label;Layout.preferredWidth:180;elide:Text.ElideRight;wrapMode:Text.NoWrap;tip:line.modelData.label}
                                            Item {
                                                Layout.fillWidth:true;Layout.preferredHeight:24
                                                Rectangle {height:22;width:parent.width*line.modelData.fraction;anchors.verticalCenter:parent.verticalCenter;radius:3;color:appTheme.color(line.modelData.color)}
                                                Copy {visible:line.modelData.missing;text:root.t('측정 불가');font.pixelSize:12;color:appTheme.palette.muted}
                                            }
                                            Copy {text:line.modelData.value+(line.modelData.partial?' *':'');Layout.preferredWidth:110;horizontalAlignment:Text.AlignRight;tip:line.modelData.tip+'\n'+line.modelData.coverage}
                                            Copy {text:root.s.meanMetric?line.modelData.coverage:line.modelData.share;Layout.preferredWidth:90;horizontalAlignment:Text.AlignRight;font.pixelSize:12;color:appTheme.palette.muted;tip:line.modelData.coverage}
                                            Copy {text:'›';Layout.preferredWidth:20;font.pixelSize:20}
                                        }
                                        RowLayout {
                                            visible:root.tableView;anchors.fill:parent;spacing:12
                                            Copy {text:line.modelData.label;Layout.preferredWidth:210;elide:Text.ElideRight;wrapMode:Text.NoWrap;tip:line.modelData.label}
                                            Repeater {
                                                model:line.modelData.metrics||[]
                                                Copy {required property var modelData;text:modelData.value+(modelData.partial?' *':'');Layout.fillWidth:true;Layout.preferredWidth:110;horizontalAlignment:Text.AlignRight;font.bold:modelData.key===root.o.metric;tip:root.t('유효 호출 / 전체')+': '+modelData.tip}
                                            }
                                            Copy {text:'›';Layout.preferredWidth:20;font.pixelSize:20}
                                        }
                                    }
                                }
                            }
                        }
                        Copy {visible:rowsView.count===0;y:41;width:parent.width;height:100;text:root.t('조건에 맞는 기록 없음');horizontalAlignment:Text.AlignHCenter;color:appTheme.palette.muted}
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
