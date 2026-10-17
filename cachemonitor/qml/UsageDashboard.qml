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
    readonly property bool splitModels: o.dimension==='model' && (o.breakdowns||[]).length>0
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
        spacing: 6
        Copy { text: caption; font.pixelSize: 12; color: appTheme.palette.muted }
        UiChoice {
            objectName: "usage-"+field
            Layout.fillWidth:true; model: entries; textRole: "text"
            currentIndex: root.selected(entries,value); Accessible.name: caption
            onActivated: root.node.change(field,model[currentIndex].value)
        }
    }
    ColumnLayout {
        id: compare
        anchors.fill:parent;spacing:14
        RowLayout {
            Layout.fillWidth:true;spacing:12
            Field {caption:root.t('조회 기간');field:'period';value:root.o.period;Layout.preferredWidth:150;Layout.maximumWidth:150;entries:[{text:root.t('전체 기록'),value:'all'},{text:root.t('오늘'),value:'today'},{text:root.t('최근 7일'),value:'7d'},{text:root.t('최근 30일'),value:'30d'},{text:root.t('직접 지정'),value:'custom'}]}
            UiInput {visible:root.o.period==='custom';Layout.preferredWidth:118;placeholderText:'YYYY-MM-DD';text:root.o.start;Accessible.name:root.t('시작일');onEditingFinished:root.node.change('start',text)}
            UiInput {visible:root.o.period==='custom';Layout.preferredWidth:118;placeholderText:'YYYY-MM-DD';text:root.o.end;Accessible.name:root.t('종료일');onEditingFinished:root.node.change('end',text)}
            UiButton {text:root.t('조회 조건');Layout.alignment:Qt.AlignBottom;onClicked:root.node.change('filtersOpen','')}
            Copy {text:root.masked?'':root.s.scope;Layout.fillWidth:true;color:appTheme.palette.muted;font.pixelSize:12;Layout.alignment:Qt.AlignBottom;Layout.bottomMargin:10}
            UiButton {text:root.t('새로고침');role:'quiet';Layout.alignment:Qt.AlignBottom;onClicked:root.node.change('metric',root.o.metric)}
        }
        Flow {
            Layout.fillWidth:true;Layout.preferredHeight:childrenRect.height;spacing:12;visible:root.o.filtersOpen
            Repeater {model:root.s.choices||[];Field {required property var modelData;width:160;caption:modelData.title;entries:modelData.items;field:'filter:'+modelData.key;value:root.o.filters[modelData.key]||''}}
            UiCheckBox {text:root.t('보관 세션 포함');checked:root.o.archived;onToggled:root.node.change('archived',checked?'true':'false')}
        }
        RowLayout {
            visible:root.o.selected.length>0
            UiButton {text:'← '+root.t('뒤로');onClicked:root.node.back()}
            UiButton {text:root.t('지표 비교');role:'quiet';onClicked:root.node.reset()}
            Copy {text:root.masked?'':'/ '+(root.s.selectedLabel||'');color:appTheme.palette.muted}
        }
        RowLayout {
            Layout.fillWidth:true;spacing:20
            Field {caption:root.t('비교 대상');field:'dimension';value:root.o.dimension;entries:root.s.dimensions;Layout.preferredWidth:160;Layout.maximumWidth:160}
            Field {caption:root.t('집계 기준');field:'metric';value:root.o.metric;entries:root.s.metrics;Layout.preferredWidth:200;Layout.maximumWidth:200}
            Item {Layout.fillWidth:true}
            RowLayout {
                visible:root.o.metric!=='tps';Layout.alignment:Qt.AlignBottom;spacing:3
                UiButton {text:root.t('총량');checkable:true;checked:root.o.mode==='amount';onClicked:root.node.change('mode','amount')}
                UiButton {text:root.t('구성비');checkable:true;checked:root.o.mode==='ratio';onClicked:root.node.change('mode','ratio')}
            }
        }
        Rectangle {Layout.fillWidth:true;height:1;color:appTheme.palette.border}
        ColumnLayout {
            visible:root.o.selected.length>0;Layout.fillWidth:true;spacing:12
            RowLayout {Layout.fillWidth:true;Copy {text:root.masked?'':root.s.selectedLabel||'';font.pixelSize:18;font.bold:true;Layout.fillWidth:true}UiButton{text:root.t('세션 기록 열기');enabled:!root.masked;onClicked:root.node.records()}}
            Item {
                Layout.fillWidth:true;Layout.preferredHeight:detailValues.childrenRect.height
                Flow {id:detailValues;width:parent.width;visible:!root.masked;spacing:24;Repeater {model:root.s.detail;Column {required property var modelData;spacing:7;Copy{text:modelData.title;font.pixelSize:12;color:appTheme.palette.muted;tip:modelData.tip}Copy{text:modelData.value+(modelData.lastTs?' / '+Math.max(0,Math.floor((root.now-modelData.lastTs)/3600))+root.t('시간 전'):'');tip:modelData.tip}}}}
            }
            Rectangle {Layout.fillWidth:true;height:1;color:appTheme.palette.border}
        }
        RowLayout {
            Layout.fillWidth:true
            Copy {text:root.masked?'':root.s.title;font.pixelSize:18;font.bold:true;Layout.fillWidth:true;tip:!root.masked && root.o.metric==='tps'?root.s.tpsTip:''}
            UiInput {Layout.preferredWidth:165;placeholderText:root.t('항목 검색');text:root.o.search;Accessible.name:root.t('항목 검색');onTextEdited:root.node.change('search',text)}
            UiChoice {Layout.preferredWidth:120;model:[root.t('큰 값부터'),root.t('작은 값부터'),root.t('이름순')];currentIndex:['desc','asc','name'].indexOf(root.o.sort);Accessible.name:root.t('항목 정렬');onActivated:root.node.change('sort',['desc','asc','name'][currentIndex])}
        }
        RowLayout {
            objectName:'usage-legend-row'
            Layout.fillWidth:true;Layout.minimumHeight:36;Layout.preferredHeight:36;Layout.maximumHeight:36;spacing:16
            Flickable {
                Layout.fillWidth:true;Layout.fillHeight:true;Layout.minimumWidth:0
                Layout.preferredWidth:legendItems.implicitWidth;Layout.maximumWidth:legendItems.implicitWidth
                contentWidth:legendItems.implicitWidth;contentHeight:height;clip:true
                flickableDirection:Flickable.HorizontalFlick;boundsBehavior:Flickable.StopAtBounds
                ScrollBar.horizontal:ScrollBar {policy:ScrollBar.AsNeeded}
                Row {
                    id:legendItems;height:parent.height;spacing:14;visible:!root.masked
                    Copy {visible:root.o.metric==='tps';anchors.verticalCenter:parent.verticalCenter;text:root.t('평균 속도 (tok/s)');color:appTheme.palette.muted;font.pixelSize:12;tip:root.s.tpsTip}
                    Repeater {model:root.s.legend;Row {required property var modelData;anchors.verticalCenter:parent.verticalCenter;spacing:6;Rectangle{width:9;height:9;anchors.verticalCenter:parent.verticalCenter;color:appTheme.color(modelData.color);radius:2}Copy{text:modelData.label;color:appTheme.palette.muted;font.pixelSize:12}}}
                }
            }
            RowLayout {
                visible:root.o.dimension==='model';Layout.minimumWidth:implicitWidth;spacing:16
                Rectangle {visible:root.o.metric==='tps'||root.s.legend.length>0;Layout.preferredWidth:1;Layout.preferredHeight:20;color:appTheme.palette.border}
                Copy {text:root.t('세분화');font.pixelSize:12;color:appTheme.palette.muted}
                UiCheckBox {objectName:'usage-breakdown-service_tier';text:root.t('요청 모드');checked:(root.o.breakdowns||[]).indexOf('service_tier')>=0;onToggled:root.node.change('breakdown:service_tier',checked?'true':'false')}
                UiCheckBox {objectName:'usage-breakdown-effort';text:root.t('추론 설정');checked:(root.o.breakdowns||[]).indexOf('effort')>=0;onToggled:root.node.change('breakdown:effort',checked?'true':'false')}
            }
            Item {Layout.fillWidth:true}
        }
        Item {
            id: chart
            objectName:'usage-chart'
            Layout.fillWidth:true;Layout.fillHeight:true;Layout.minimumHeight:148
            readonly property bool canResize: visible && !root.s.queryPending && !root.masked
            onHeightChanged:if(canResize)resize.restart()
            onCanResizeChanged:if(canResize)resize.restart();else resize.stop()
            Timer {id:resize;objectName:'usage-resize';interval:100;onTriggered:if(chart.canResize)root.node.change('size',String(Math.floor((chart.height-36)/54)))}
            Column {
                objectName:'usage-chart-content'
                visible:!root.masked;enabled:!root.masked
                anchors.left:parent.left;anchors.right:parent.right;anchors.top:parent.top;spacing:0
                RowLayout {
                    width:parent.width;height:34;spacing:16
                    Copy {text:root.t('항목');Layout.preferredWidth:root.splitModels?180:150;font.pixelSize:12;color:appTheme.palette.muted}
                    RowLayout {Layout.fillWidth:true;Copy{text:'0';font.pixelSize:12;color:appTheme.palette.muted}Item{Layout.fillWidth:true}Copy{text:root.s.middle||'';font.pixelSize:12;color:appTheme.palette.muted}Item{Layout.fillWidth:true}Copy{text:root.s.upper||'';font.pixelSize:12;color:appTheme.palette.muted}}
                    Copy {text:root.o.metric==='tps'?root.t('평균 tok/s'):root.s.metricLabel||'';Layout.preferredWidth:110;font.pixelSize:12;horizontalAlignment:Text.AlignRight;color:appTheme.palette.muted;tip:root.o.metric==='tps'?root.s.tpsTip:''}
                    Copy {text:root.o.metric==='tps'?root.t('유효 호출 / 전체'):root.s.shareLabel||'';Layout.preferredWidth:90;font.pixelSize:12;horizontalAlignment:Text.AlignRight;color:appTheme.palette.muted;tip:root.t('해당 항목의 값 ÷ 선택 범위 전체 값입니다. 검색과 페이지 이동은 분모를 바꾸지 않습니다.')}
                    Item {Layout.preferredWidth:20}
                }
                Rectangle {width:parent.width;height:1;color:appTheme.palette.border}
                Repeater {
                    model:root.s.rows
                    Column {
                        id:groupLine
                        required property var modelData
                        width:parent.width
                        Copy {visible:groupLine.modelData.groupStart;width:parent.width;height:28;text:groupLine.modelData.group;font.pixelSize:13;font.bold:true;elide:Text.ElideRight;wrapMode:Text.NoWrap;tip:groupLine.modelData.group}
                        AbstractButton {
                            id: line
                            objectName: "usage-row-"+modelData.key
                            readonly property var modelData:groupLine.modelData
                            width:parent.width;height:54;hoverEnabled:true;activeFocusOnTab:true;Accessible.name:(root.splitModels?modelData.group+' / ':'')+modelData.label+' '+modelData.value
                            onClicked:root.node.select(modelData.key)
                            background:Rectangle {color:line.hovered||line.activeFocus?appTheme.palette.secondary:'transparent';Rectangle {anchors.bottom:parent.bottom;width:parent.width;height:1;color:appTheme.palette.border;opacity:.4}}
                            contentItem:RowLayout {
                                spacing:16
                                Copy {text:line.modelData.label;Layout.preferredWidth:root.splitModels?180:150;leftPadding:root.splitModels?14:0;elide:Text.ElideRight;wrapMode:Text.NoWrap;tip:line.modelData.label}
                                Item {
                                    Layout.fillWidth:true;Layout.preferredHeight:24
                                    Rectangle {visible:root.o.metric==='tps';height:22;width:parent.width*line.modelData.fraction;anchors.verticalCenter:parent.verticalCenter;radius:3;color:appTheme.color(line.modelData.color)}
                                    Row {
                                        visible:root.o.metric!=='tps';height:22;width:parent.width*line.modelData.fraction;anchors.verticalCenter:parent.verticalCenter
                                        Repeater {model:line.modelData.segments;Rectangle {required property var modelData;property real share:modelData.width;width:parent.width*share;height:22;color:appTheme.color(modelData.color);HoverHandler{id:segHover}UiToolTip{visible:segHover.hovered;text:parent.modelData.tip}Repeater{model:parent.modelData.unknown?Math.floor(parent.width/7):0;Rectangle{x:index*7;width:2;height:22;rotation:30;color:appTheme.palette.surface;opacity:.7}}}}
                                    }
                                    Copy {visible:line.modelData.missing;text:root.t(root.o.metric==='tps'?'측정 불가':'미확인');font.pixelSize:12;color:appTheme.palette.muted}
                                }
                                Column {
                                    Layout.preferredWidth:110
                                    Copy {width:parent.width;text:line.modelData.value;horizontalAlignment:Text.AlignRight;tip:line.modelData.tip}
                                    Copy {visible:line.modelData.partial&&root.o.metric!=='tps';width:parent.width;text:root.t('확인분');font.pixelSize:10;color:appTheme.palette.muted;horizontalAlignment:Text.AlignRight;tip:line.modelData.coverage}
                                }
                                Copy {text:root.o.metric==='tps'?line.modelData.coverage:line.modelData.share;Layout.preferredWidth:90;horizontalAlignment:Text.AlignRight;font.pixelSize:12;color:appTheme.palette.muted;tip:line.modelData.coverage}
                                Copy {text:'›';Layout.preferredWidth:20;font.pixelSize:20}
                            }
                        }
                    }
                }
                Copy {visible:root.s.rows.length===0;width:parent.width;height:100;text:root.t('조건에 맞는 기록 없음');horizontalAlignment:Text.AlignHCenter;color:appTheme.palette.muted}
            }
            Loader { anchors.fill:parent;active:!!root.loading.active;sourceComponent:Component { AnalysisLoading { status:root.loading } } }
        }
        RowLayout {
            Layout.fillWidth:true
            Copy {text:root.masked?'':root.s.countLabel;Layout.fillWidth:true;font.pixelSize:12;color:appTheme.palette.muted}
            UiButton {text:root.t('이전');enabled:!root.masked && !!root.s.previous;onClicked:root.node.change('page','-1')}
            Copy {text:root.masked?'':root.s.pageLabel;font.pixelSize:12;color:appTheme.palette.muted}
            UiButton {text:root.t('다음');enabled:!root.masked && !!root.s.next;onClicked:root.node.change('page','1')}
        }
    }
}
