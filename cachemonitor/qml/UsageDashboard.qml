import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var node
    readonly property var s: node.state
    readonly property var o: s.options
    property real now: Date.now()/1000
    Timer {interval:60000;repeat:true;running:root.visible && !root.s.home && root.o.selected.length>0;onTriggered:root.now=Date.now()/1000}
    function t(text) { return appLanguage.text(text) }
    function selected(items,value) { for(let i=0;i<items.length;i++) if(items[i].value===value)return i;return 0 }
    component Copy: Text {
        property string tip: ""
        color: appTheme.palette.ink; font.family: appTheme.family; font.pixelSize: 14
        wrapMode: Text.Wrap; verticalAlignment: Text.AlignVCenter
        HoverHandler { id: hover }
        UiToolTip { visible: hover.hovered && parent.tip.length>0; text: parent.tip }
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
        anchors.fill: parent; spacing: 20; visible: root.s.home
        Rectangle {
            Layout.fillWidth:true; implicitHeight:118; color:appTheme.palette.surface; radius:10;border.color:appTheme.palette.border
            RowLayout {
                anchors.fill:parent;anchors.margins:16;spacing:20
                Repeater {
                    model: root.s.totals
                    ColumnLayout {
                        required property var modelData
                        Layout.fillWidth:true;Layout.preferredWidth:1;spacing:8
                        Copy { text:modelData.title;color:appTheme.palette.muted;font.pixelSize:13;tip:modelData.tip }
                        RowLayout {
                            spacing:5
                            Copy { text:modelData.value;font.pixelSize:28;font.bold:true;tip:modelData.tip }
                            Copy { text:modelData.unit;font.pixelSize:12;color:appTheme.palette.muted }
                            Copy { visible:modelData.partial;text:root.t('확인분');font.pixelSize:11;color:appTheme.palette.muted;tip:modelData.tip }
                        }
                        Copy { text:modelData.sub;font.pixelSize:12;color:appTheme.palette.muted;tip:modelData.subTip;Layout.minimumHeight:16 }
                    }
                }
            }
        }
        GridLayout {
            Layout.fillWidth:true;Layout.fillHeight:true;columns:2;rowSpacing:20;columnSpacing:20
            Repeater {
                model:root.s.cards
                AbstractButton {
                    id: card
                    objectName: "home-card-"+modelData.page
                    required property var modelData
                    Layout.fillWidth:true;Layout.fillHeight:true;Layout.preferredWidth:1;Layout.preferredHeight:1;Layout.minimumHeight:205
                    hoverEnabled:true;activeFocusOnTab:true;Accessible.name:root.t(modelData.title)+', '+root.t(modelData.description)
                    onClicked:root.node.navigate(modelData.page)
                    background:Rectangle {radius:10;color:card.hovered?appTheme.palette.secondary:appTheme.palette.surface;border.color:card.activeFocus||card.hovered?appTheme.palette.accent:appTheme.palette.border}
                    contentItem:ColumnLayout {
                        anchors.fill:parent;anchors.margins:24;spacing:14
                        Rectangle {
                            width:36;height:36;radius:7;color:appTheme.palette.secondary
                            Canvas {
                                anchors.centerIn:parent;width:26;height:26
                                property color ink:appTheme.palette.accent
                                onInkChanged:requestPaint()
                                onPaint:{let c=getContext('2d');c.reset();c.strokeStyle=ink;c.lineWidth=1.7;c.beginPath();
                                    if(card.modelData.icon==='compare'){c.moveTo(4,6);c.lineTo(23,6);c.moveTo(4,13);c.lineTo(17,13);c.moveTo(4,20);c.lineTo(11,20)}
                                    else if(card.modelData.icon==='trend'){c.moveTo(3,3);c.lineTo(3,23);c.lineTo(24,23);c.moveTo(6,18);c.lineTo(11,11);c.lineTo(16,15);c.lineTo(23,5)}
                                    else if(card.modelData.icon==='records'){c.rect(5,2,17,22);for(let y=7;y<22;y+=5){c.moveTo(9,y);c.lineTo(18,y)}}
                                    else{c.arc(13,13,10,.7,5.6);c.moveTo(13,13);c.lineTo(19,7)}c.stroke()}
                            }
                        }
                        Copy {text:root.t(card.modelData.title);font.pixelSize:24;font.bold:true;Layout.fillWidth:true}
                        Copy {text:root.t(card.modelData.description);font.pixelSize:18;Layout.fillWidth:true}
                        Item {Layout.fillHeight:true}
                        RowLayout {Layout.fillWidth:true;Copy {text:root.t(card.modelData.title)+' '+root.t('열기');font.pixelSize:15;font.bold:true}Item{Layout.fillWidth:true}Copy{text:'→';font.pixelSize:22}}
                    }
                }
            }
        }
    }
    ColumnLayout {
        id: compare
        anchors.fill:parent;visible:!root.s.home;spacing:14
        RowLayout {
            Layout.fillWidth:true;spacing:12
            Field {caption:root.t('조회 기간');field:'period';value:root.o.period;Layout.preferredWidth:150;Layout.maximumWidth:150;entries:[{text:root.t('전체 기록'),value:'all'},{text:root.t('오늘'),value:'today'},{text:root.t('최근 7일'),value:'7d'},{text:root.t('최근 30일'),value:'30d'},{text:root.t('직접 지정'),value:'custom'}]}
            UiInput {visible:root.o.period==='custom';Layout.preferredWidth:118;placeholderText:'YYYY-MM-DD';text:root.o.start;Accessible.name:root.t('시작일');onEditingFinished:root.node.change('start',text)}
            UiInput {visible:root.o.period==='custom';Layout.preferredWidth:118;placeholderText:'YYYY-MM-DD';text:root.o.end;Accessible.name:root.t('종료일');onEditingFinished:root.node.change('end',text)}
            UiButton {text:root.t('조회 조건');Layout.alignment:Qt.AlignBottom;onClicked:root.node.change('filtersOpen','')}
            Copy {text:root.s.scope;Layout.fillWidth:true;color:appTheme.palette.muted;font.pixelSize:12;Layout.alignment:Qt.AlignBottom;Layout.bottomMargin:10}
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
            Copy {text:'/ '+(root.s.selectedLabel||'');color:appTheme.palette.muted}
        }
        RowLayout {
            Layout.fillWidth:true;spacing:20
            Field {caption:root.t('비교 대상');field:'dimension';value:root.o.dimension;entries:root.s.dimensions;Layout.preferredWidth:160;Layout.maximumWidth:160}
            Field {caption:root.t('집계 기준');field:'metric';value:root.o.metric;entries:root.s.metrics;Layout.preferredWidth:200;Layout.maximumWidth:200}
            Item {Layout.fillWidth:true}
            RowLayout {
                visible:root.o.metric!=='tps';Layout.alignment:Qt.AlignBottom;spacing:3
                UiButton {text:root.t('실제 규모');checkable:true;checked:root.o.mode==='amount';onClicked:root.node.change('mode','amount')}
                UiButton {text:root.t('내부 구성비');checkable:true;checked:root.o.mode==='ratio';onClicked:root.node.change('mode','ratio')}
            }
        }
        Rectangle {Layout.fillWidth:true;height:1;color:appTheme.palette.border}
        ColumnLayout {
            visible:root.o.selected.length>0;Layout.fillWidth:true;spacing:12
            RowLayout {Layout.fillWidth:true;Copy {text:root.s.selectedLabel||'';font.pixelSize:18;font.bold:true;Layout.fillWidth:true}UiButton{text:root.t('세션 기록 열기');onClicked:root.node.records()}}
            Flow {Layout.fillWidth:true;Layout.preferredHeight:childrenRect.height;spacing:24;Repeater {model:root.s.detail;Column {required property var modelData;spacing:7;Copy{text:modelData.title;font.pixelSize:12;color:appTheme.palette.muted;tip:modelData.tip}Copy{text:modelData.value+(modelData.lastTs?' / '+Math.max(0,Math.floor((root.now-modelData.lastTs)/3600))+root.t('시간 전'):'');tip:modelData.tip}}}}
            Rectangle {Layout.fillWidth:true;height:1;color:appTheme.palette.border}
        }
        RowLayout {
            Layout.fillWidth:true
            Copy {text:root.s.title;font.pixelSize:18;font.bold:true;Layout.fillWidth:true;tip:root.o.metric==='tps'?root.s.tpsTip:''}
            UiInput {Layout.preferredWidth:165;placeholderText:root.t('항목 검색');text:root.o.search;Accessible.name:root.t('항목 검색');onTextEdited:root.node.change('search',text)}
            UiChoice {Layout.preferredWidth:120;model:[root.t('큰 값부터'),root.t('작은 값부터'),root.t('이름순')];currentIndex:['desc','asc','name'].indexOf(root.o.sort);Accessible.name:root.t('항목 정렬');onActivated:root.node.change('sort',['desc','asc','name'][currentIndex])}
        }
        Flow {
            Layout.fillWidth:true;Layout.preferredHeight:Math.max(20,childrenRect.height);spacing:14
            Copy {visible:root.o.metric==='tps';text:root.t('평균 속도 (tok/s)');color:appTheme.palette.muted;font.pixelSize:12;tip:root.s.tpsTip}
            Repeater {model:root.s.legend;Row {required property var modelData;spacing:6;Rectangle{width:9;height:9;anchors.verticalCenter:parent.verticalCenter;color:appTheme.color(modelData.color);radius:2}Copy{text:modelData.label;color:appTheme.palette.muted;font.pixelSize:12}}}
        }
        Item {
            id: chart
            Layout.fillWidth:true;Layout.fillHeight:true;Layout.minimumHeight:148
            onHeightChanged:if(!root.s.home)resize.restart()
            Timer {id:resize;interval:100;onTriggered:root.node.change('size',String(Math.floor((chart.height-36)/54)))}
            Column {
                anchors.fill:parent;spacing:0
                RowLayout {
                    width:parent.width;height:34;spacing:16
                    Copy {text:root.t('항목');Layout.preferredWidth:150;font.pixelSize:12;color:appTheme.palette.muted}
                    RowLayout {Layout.fillWidth:true;Copy{text:'0';font.pixelSize:12;color:appTheme.palette.muted}Item{Layout.fillWidth:true}Copy{text:root.s.middle||'';font.pixelSize:12;color:appTheme.palette.muted}Item{Layout.fillWidth:true}Copy{text:root.s.upper||'';font.pixelSize:12;color:appTheme.palette.muted}}
                    Copy {text:root.o.metric==='tps'?root.t('평균 tok/s'):root.s.metricLabel||'';Layout.preferredWidth:110;font.pixelSize:12;horizontalAlignment:Text.AlignRight;color:appTheme.palette.muted;tip:root.o.metric==='tps'?root.s.tpsTip:''}
                    Copy {text:root.o.metric==='tps'?root.t('유효 호출 / 전체'):root.s.shareLabel||'';Layout.preferredWidth:90;font.pixelSize:12;horizontalAlignment:Text.AlignRight;color:appTheme.palette.muted;tip:root.t('해당 항목의 값 ÷ 선택 범위 전체 값입니다. 검색과 페이지 이동은 분모를 바꾸지 않습니다.')}
                    Item {Layout.preferredWidth:20}
                }
                Rectangle {width:parent.width;height:1;color:appTheme.palette.border}
                Repeater {
                    model:root.s.rows
                    AbstractButton {
                        id: line
                        objectName: "usage-row-"+modelData.key
                        required property var modelData
                        width:parent.width;height:54;hoverEnabled:true;activeFocusOnTab:true;Accessible.name:modelData.label+' '+modelData.value
                        onClicked:root.node.select(modelData.key)
                        background:Rectangle {color:line.hovered||line.activeFocus?appTheme.palette.secondary:'transparent';Rectangle {anchors.bottom:parent.bottom;width:parent.width;height:1;color:appTheme.palette.border;opacity:.4}}
                        contentItem:RowLayout {
                            spacing:16
                            Copy {text:line.modelData.label;Layout.preferredWidth:150;elide:Text.ElideRight;wrapMode:Text.NoWrap;tip:line.modelData.label}
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
                Copy {visible:root.s.rows.length===0;width:parent.width;height:100;text:root.t('조건에 맞는 기록 없음');horizontalAlignment:Text.AlignHCenter;color:appTheme.palette.muted}
            }
        }
        RowLayout {
            Layout.fillWidth:true
            Copy {text:root.s.countLabel;Layout.fillWidth:true;font.pixelSize:12;color:appTheme.palette.muted}
            UiButton {text:root.t('이전');enabled:!!root.s.previous;onClicked:root.node.change('page','-1')}
            Copy {text:root.s.pageLabel;font.pixelSize:12;color:appTheme.palette.muted}
            UiButton {text:root.t('다음');enabled:!!root.s.next;onClicked:root.node.change('page','1')}
        }
    }
}
