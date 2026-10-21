"""Metric comparison presentation, backed by worker projections."""
from copy import deepcopy
from collections import OrderedDict
from datetime import datetime, timedelta
from PySide6.QtCore import Slot, QTimer, QAbstractListModel, QModelIndex, Qt, Property, QObject
from .presentation import Node
from .i18n import tr, language
from .theme import shared_theme

DIMENSIONS = [('전체','all'),('프로젝트','project'),('작업 종류','source'),('요청 모드','service_tier'),
              ('입력 토큰 규모','input_band'),('캐시 활용 구간','cache_band'),('수집 위치','home')]
BREAKDOWNS = [('요청 모드','service_tier'),('추론 설정','effort')]
METRICS = [('호출 수','calls'),('구독 가치 환산액','cost'),('호출당 환산액','cost_per_call'),
           ('평균 응답시간','latency'),('호출당 평균 TPS','tps'),
           ('호출당 입력 토큰','input_per_call'),('호출당 출력 토큰','output_per_call'),('호출당 추론 토큰','reasoning')]
NAMES={'user':'유저 에이전트','subagent':'하위 에이전트','unknown':'미확인'}
BOX_LEGEND='상자 Q1~Q3 / 선 중앙값 / 수염 P10~P90 / 점 평균'
BOX_TIP='상자는 가운데 50%인 Q1~Q3, 선은 중앙값, 수염은 P10~P90, 점은 산술평균입니다. 유효 호출을 동일 가중으로 계산합니다.'
TPS='각 호출의 출력 토큰을 전체 응답시간(초)으로 나눈 값의 산술평균입니다. 출력에는 추론이, 시간에는 대기와 통신이 포함됩니다. 유효한 완료 호출만 동일 가중으로 평균합니다.'


def value_text(value, metric):
    if value is None:return '—'
    if metric in ('cost','cost_per_call'):return f'${value:,.4f}' if metric=='cost_per_call' else f'${value:,.2f}'
    if metric=='latency':return f'{value:,.1f}s'
    if metric=='tps':return f'{value:,.1f}'
    if metric=='reasoning' and value<1000:return f'{value:,.1f}'
    if metric=='calls':return f'{round(value):,}'
    if value>=1e9:return f'{value/1e9:.2f}B'
    return f'{value/1e6:.2f}M' if value>=1e6 else f'{value/1000:.1f}k' if value>=1000 else f'{value:,}'


class UsageRows(QAbstractListModel):
    """Share aggregate rows; cache at most 128 formatted rows within 4 MiB."""
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.rows=[];self.cache=OrderedDict();self.cache_bytes=0;self.format_key=None

    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.rows)
    def roleNames(self):return {Qt.UserRole:b'modelData'}
    def data(self,index,role=Qt.DisplayRole):
        if role!=Qt.UserRole or not index.isValid() or not 0<=index.row()<len(self.rows):return None
        row=index.row()
        if row not in self.cache:
            from .analysis_engine import retained_bytes
            value=self.owner.format_row(row);size=retained_bytes(value,4*1024*1024)
            if size>4*1024*1024:return value
            while self.cache and (len(self.cache)>=128 or self.cache_bytes+size>4*1024*1024):
                _,(_,removed)=self.cache.popitem(last=False);self.cache_bytes-=removed
            self.cache[row]=(value,size);self.cache_bytes+=size
        self.cache.move_to_end(row)
        return self.cache[row][0]

    def replace(self,rows,format_key):
        if self.format_key==format_key and self.rows==rows:
            self.rows=rows;return
        same_keys=len(rows)==len(self.rows) and all(a['key']==b['key'] for a,b in zip(rows,self.rows))
        if not same_keys:self.beginResetModel()
        self.rows=rows;self.cache.clear();self.cache_bytes=0;self.format_key=format_key
        if not same_keys:self.endResetModel()
        elif rows:self.dataChanged.emit(self.index(0),self.index(len(rows)-1),[Qt.UserRole])


class UsagePanel(Node):
    kind='usageDashboard'

    def __init__(self, dashboard):
        super().__init__();self.dashboard=dashboard;self.data={};self.row_model=UsageRows(self)
        self.options=dict(period='all',start='',end='',filters={},archived=True,dimension='project',metric='calls',
                          mode='amount',search='',sort='desc',selected=[],views={},filtersOpen=False,breakdowns=[],view='chart',showUnknown=False)
        self.date_draft={key:self.options[key] for key in ('period','start','end')}
        self.search_timer=QTimer(self);self.search_timer.setSingleShot(True);self.search_timer.setInterval(160)
        self.search_timer.timeout.connect(self.present)
        shared_theme().changed.connect(self.restyle)
        self.put(options=deepcopy(self.options),
            dimensions=[dict(text=tr(t),value=v) for t,v in DIMENSIONS],metrics=[dict(text=tr(t),value=v) for t,v in METRICS],
            legend=[],detail=[],scope=tr('전체 기록'),countLabel='',title='',maximum=1,tpsTip='',metricLabel='')
        self.publish_dates()

    @Property(QObject,constant=True)
    def rowModel(self):return self.row_model

    def restyle(self):
        self.publish_dates()
        if self.data:self.present()

    def capture(self):return deepcopy(self.options)

    @staticmethod
    def custom_range(start,end):
        try:
            first=datetime.fromisoformat(start);last=datetime.fromisoformat(end)
            if first.date().isoformat()!=start or last.date().isoformat()!=end:raise ValueError
            bounds=(first.timestamp(),(last+timedelta(days=1)).timestamp())
        except (ValueError,TypeError,OverflowError,OSError):
            raise ValueError('시작일과 종료일을 YYYY-MM-DD 형식으로 입력하세요.') from None
        if first>last:raise ValueError('종료일은 시작일보다 빠를 수 없습니다.')
        return bounds

    def publish_dates(self):
        draft=self.date_draft;custom=draft['period']=='custom';error=''
        if custom:
            try:self.custom_range(draft['start'],draft['end'])
            except ValueError as exc:error=tr(str(exc))
        pending=custom and any(draft[key]!=self.options[key] for key in ('period','start','end'))
        self.put(dateDraft=deepcopy(draft),dateError=error,datePending=pending,
                 dateCanApply=bool(pending and not error),
                 dateNotice=error or (tr('날짜를 적용하면 조회 기간이 변경됩니다.') if pending else ''))

    def restore(self,value):
        restored=deepcopy({k:v for k,v in value.items() if k in self.options})
        dates={key:restored.get(key,self.options[key]) for key in ('period','start','end')}
        if dates['period'] not in ('all','today','7d','30d','custom'):dates['period']='all'
        restored['period']=dates['period']
        try:self.custom_range(dates['start'],dates['end'])
        except ValueError:
            empty=dates['period']!='custom' and dates['start']==dates['end']==''
            restored.update({key:'' if empty else self.options[key] for key in ('start','end')})
            if dates['period']=='custom':restored['period']=self.options['period']
        else:restored.update(dates)
        self.options.update(restored)
        self.options['showUnknown']=self.options['showUnknown'] is True
        self.date_draft=dates if dates['period']=='custom' else {key:self.options[key] for key in ('period','start','end')}
        o=self.options;legacy=o['dimension']
        if legacy not in dict((v,t) for t,v in DIMENSIONS):o['dimension']='all'
        if o['metric'] not in dict((v,t) for t,v in METRICS):o['metric']='calls'
        o['views']={dimension:{k:v for k,v in view.items() if k in ('search','sort')}
                    for dimension,view in o['views'].items()}
        o['breakdowns']=[key for _,key in BREAKDOWNS if key in o['breakdowns']]
        self.put(options=deepcopy(o));self.publish_dates()

    def refresh(self):
        self.dashboard.render();self.dashboard.save_preferences()

    def present(self):
        if getattr(self,'_all_data',None):self.apply(self._all_data)
        self.dashboard.save_preferences()

    @Slot(str,str)
    def change(self,key,value):
        o=self.options
        if key in ('start','end'):
            self.date_draft[key]=value;self.publish_dates();return
        if key=='period':
            if value not in ('all','today','7d','30d','custom'):return
            if value=='custom':
                self.date_draft['period']=value;self.publish_dates();return
            self.dashboard.push_state();o['period']=value
            self.date_draft={key:o[key] for key in ('period','start','end')};self.publish_dates()
        elif key=='applyDates':
            self.publish_dates()
            if not self.state['dateCanApply']:return
            self.dashboard.push_state();o.update(self.date_draft);self.publish_dates()
        elif key=='filtersOpen':o[key]=not o[key];self.put(options=deepcopy(o));return
        elif key=='search':
            o['search']=value;self.put(options=deepcopy(o));self.search_timer.start();return
        elif key=='dimension':
            if value not in dict((v,t) for t,v in DIMENSIONS):return
            o['views'][o['dimension']]={k:o[k] for k in ('search','sort')}
            o['dimension']=value;o.update(o['views'].get(value,dict(search='',sort='desc')))
        elif key.startswith('breakdown:'):
            field=key.split(':',1)[1]
            if field not in dict((v,t) for t,v in BREAKDOWNS):return
            selected=set(o['breakdowns'])
            if value=='true':selected.add(field)
            else:selected.discard(field)
            o['breakdowns']=[key for _,key in BREAKDOWNS if key in selected]
        elif key=='archived':o['archived']=value=='true'
        elif key=='showUnknown':
            o[key]=value=='true';self.put(options=deepcopy(o));self.present();return
        elif key.startswith('filter:'):
            self.dashboard.push_state();field=key.split(':',1)[1]
            if value:o['filters'][field]=value
            else:o['filters'].pop(field,None)
            o['selected']=[]
        elif key in ('mode','sort','view'):
            if key=='view' and value not in ('chart','table'):return
            o[key]=value;self.put(options=deepcopy(o));self.present();return
        elif key=='metric':
            if value not in dict((v,t) for t,v in METRICS):return
            o[key]=value;self.put(options=deepcopy(o));self.present();return
        elif key=='refresh':pass
        else:return
        self.search_timer.stop();self.put(options=deepcopy(o));self.refresh()

    @Slot()
    def back(self):self.dashboard.go_back()

    @Slot()
    def reset(self):
        self.dashboard.push_state();self.options.update(filters={},selected=[],dimension='project',breakdowns=[],search='');self.put(options=deepcopy(self.options));self.refresh()

    @Slot(str)
    def select(self,key):
        row=next((row for row in self.data.get('rows',[]) if row['key']==key),None)
        if row is None:return
        if self.data.get('comparison'):
            self.dashboard.push_state();o=self.options;o['filters'].update(row['filters'])
            o.update(selected=[v for pair in row['filters'].items() for v in pair],dimension='all',search='')
            self.put(options=deepcopy(o));self.refresh();return
        self.dashboard.push_state();o=self.options;dimension=o['dimension'];o['filters'].update(row['filters'])
        o['selected']=[value for pair in row['filters'].items() for value in pair];o['dimension']='source' if dimension=='model' else 'model'
        if o['dimension'] in o['filters']:o['dimension']=next((d for _,d in DIMENSIONS if d not in o['filters']),'model')
        o.update(search='');self.put(options=deepcopy(o));self.refresh()

    @Slot()
    def records(self):
        self.dashboard.aggregate_records=self.data.get('population');self.dashboard.open_aggregate_records()

    def query(self):
        now=self.dashboard.snapshot['ts'];o=self.options;period=o['period']
        today=datetime.fromtimestamp(now).replace(hour=0,minute=0,second=0,microsecond=0)
        start=0;end=now+.000001
        if period=='today':start=today.timestamp()
        elif period in ('7d','30d'):start=(today-timedelta(days=6 if period=='7d' else 29)).timestamp()
        elif period=='custom':
            start,finish=self.custom_range(o['start'],o['end']);end=max(start,min(end,finish))
        filters=dict(o['filters'])
        if filters.get('source')=='미확인':filters['source']='unknown'
        return dict(page=1,category_usage=True,presentation=True,include_whole_history=False,
                    start=start,end=end,now=now,period=period,archived=o['archived'],
                    loading=bool(self.dashboard.snapshot.get('index',{}).get('loading')),**filters,
                    usage=dict(comparison=True,metric='calls',**{k:o[k] for k in ('dimension','selected')}))

    def apply(self,data):
        from .category_usage import display_rows,unknown_category
        self._all_data=data
        if data.get('comparison'):
            from .comparison_usage import choose_metric,mark_bound
            data=choose_metric(data,self.options['metric'])
            data=dict(data,rows=[row for row in data['rows'] if row['value'] is not None])
        rows=display_rows(data['rows'],self.options,data.get('breakdowns',()))
        data=dict(data,rows=rows,count=len(rows),maximum=max([r['value'] or 0 for r in rows]+[0]))
        if data.get('comparison'):data['markMaximum']=mark_bound(rows,data['metric'])
        self.data=data;o=self.options;theme=shared_theme()
        from .comparison_usage import MEANS
        metric=data['metric'];ratio=o['mode']=='ratio' and metric not in MEANS;total=data['summary'];maximum=data.get('markMaximum',max(1,data['maximum']*1.05))
        if data['dimension']=='model':theme.register_models([row['filters']['model'] for row in data['rows']])
        legends={}
        for row in data['rows']:
            for c in row['components']:
                key=c['key'];color='unknown' if key=='미확인' else 'muted' if key=='기타' else theme.model_color(key) if data['cross']=='model' else 'accent' if key=='user' else 'reasoning'
                legends[key]=dict(label=tr(NAMES.get(key,key)),color=color)
        choices=[];labels=self.dashboard.snapshot.get('filter_choices',{}).get('project_labels',{})
        for caption,key in [('수집 위치','home'),('프로젝트','project'),('모델','model'),
                            ('작업 종류','source'),('요청 모드','service_tier'),('추론 강도','effort')]:
            values=list(self.dashboard.filter_values.get(key,[]))
            selected=o['filters'].get(key)
            if selected and selected not in values:values.append(selected)
            if not o['showUnknown']:values=[v for v in values if v==selected or not unknown_category(key,v)]
            choices.append(dict(key=key,title=tr(caption),items=[dict(text=tr('전체'),value='')]+[dict(text=labels.get(v,v) if key=='project' else tr(NAMES.get(v,v)),value=v) for v in values]))
        detail=[];item=data['detail'];selected_label=''
        if item:
            selected_label=' / '.join(labels.get(v,v) if k=='project' else tr(NAMES.get(v,v)) for k,v in item['selection'].items())
            detail=[dict(title=tr('세션 수'),value=f"{item['sessions']:,}",tip=tr('선택 범위에서 호출이 관측된 고유 세션 수입니다. 모델 간 세션 수는 중복될 수 있습니다.')),
                    dict(title=tr('유저 에이전트 / 하위 에이전트'),value=f"{item['sources']['user']} / {item['sources']['subagent']}"+(f" / {tr('미확인')} {item['sources']['미확인']}" if item['sources']['미확인'] else ''),tip='')]
            if item['dimension'] in ('model','source'):detail.append(dict(title=tr('관련 프로젝트 수'),value=str(item['projects'])+(' '+tr('확인분') if item['unknown_projects'] else ''),tip=tr('프로젝트 미확인 호출')+f" {item['unknown_projects']}"))
            for field,title in [('first','최초 관측'),('last','최근 사용')]:
                ts=item[field];value=datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M') if ts else '—'
                detail.append(dict(title=tr(title),value=value,lastTs=ts if field=='last' else 0,tip=tr('전체 보유 기록의 관측 시각입니다. 조회 기간과 무관하며 실제 사용 시작일이나 프로젝트 생성일이 아닙니다.')))
        elif o['selected']:
            selected_label=' / '.join(labels.get(v,tr(NAMES.get(v,v))) for v in o['selected'][1::2])
        periods={'all':'전체 기록','today':'오늘','7d':'최근 7일','30d':'최근 30일','custom':o['start']+' ~ '+o['end']}
        scope=tr(periods[o['period']])+' / '+(' / '.join(labels.get(v,tr(NAMES.get(v,v))) for v in o['filters'].values()) or tr('모든 관측 기록'))
        self.put(options=deepcopy(o),legend=[] if metric=='tps' else list(legends.values()),choices=choices,detail=detail,selectedLabel=selected_label,
                 dimensions=[dict(text=tr(t),value=v) for t,v in DIMENSIONS if v!='home' or len(self.dashboard.filter_values.get('home',[]))>1],
                 comparison=bool(data.get('comparison')),meanMetric=metric in MEANS,
                 boxLegend=tr(BOX_LEGEND),boxTip=tr(BOX_TIP),
                 scope=scope+(' / '+tr('보관 제외') if not o['archived'] else ''),title=tr(dict((v,t) for t,v in DIMENSIONS)[data['dimension']])+tr('별 ')+tr(dict((v,t) for t,v in METRICS)[metric]),
                 metric=metric,tpsTip=tr(TPS),metricLabel=tr(dict((v,t) for t,v in METRICS)[metric]),
                 shareLabel=tr('내부 구성비' if item else '전체 비중')+(' ('+tr('확인분')+')' if total['partial'] else ''),
                 middle='50%' if ratio else value_text(maximum/2,metric),upper='100%' if ratio else value_text(maximum,metric),
                 countLabel=f"{tr('항목')} {data['count']:,}")
        self.row_model.replace(data['rows'],(data['dimension'],metric,ratio,data['cross'],maximum,total['value'],
                                            tuple(data['breakdowns']),theme.appearance,language()))

    def format_row(self,index):
        data=self.data;o=self.options;theme=shared_theme();row=data['rows'][index]
        previous_group=data['rows'][index-1]['group'] if index else None
        from .comparison_usage import MEANS
        metric=data['metric'];ratio=o['mode']=='ratio' and metric not in MEANS;total=data['summary'];maximum=data.get('markMaximum',max(1,data['maximum']*1.05))
        if data.get('comparison'):
            metrics=[]
            for title,key in METRICS:
                cell=row['metrics'][key];value=cell['value']
                text=f'{round(value):,}' if key=='output_per_call' and value is not None and value<1000 else value_text(value,key)
                metrics.append(dict(key=key,title=tr(title),value=text,partial=cell['partial'],
                                    tip=f"{cell['known']:,} / {cell['total']:,}"))
            group=row.get('group_label',row['group']);descriptor=row.get('plot');plot=None
            tip=tr(TPS) if metric=='tps' else ''
            if descriptor:
                plot=dict(kind='box',mean=row['value']/maximum,
                          **{key:descriptor[key]/maximum for key in ('p10','q1','median','q3','p90')})
                stats=[tr('평균')+' '+value_text(row['value'],metric)]
                stats.extend((tr('중앙값') if key=='median' else key.upper())+' '+value_text(descriptor[key],metric)
                             for key in ('p10','q1','median','q3','p90'))
                tip='\n'.join([tr(BOX_TIP),' / '.join(stats),tr('유효 호출 / 전체')+f" {row['known']:,} / {row['total']:,}",tip]).rstrip()
            return dict(key=row['key'],label=row['label'],group=tr(NAMES.get(group,group)),
                        groupStart=row['group']!=previous_group,metrics=metrics,
                        value=value_text(row['value'],metric),missing=row['value'] is None,partial=row['partial'],
                        fraction=(row['value'] or 0)/(total['value'] or 1) if ratio else (row['value'] or 0)/maximum,
                        share=f"{row['value']/total['value']:.1%}" if metric not in MEANS and row['value'] is not None and total['value'] else '',
                        coverage=f"{row['known']:,} / {row['total']:,}",segments=[],
                        color=theme.model_color(row['filters']['model']),plot=plot,tip=tip)
        segments=[]
        for c in row['components']:
            key=c['key'];color='unknown' if key=='미확인' else 'muted' if key=='기타' else theme.model_color(key) if data['cross']=='model' else 'accent' if key=='user' else 'reasoning'
            fraction=(c['value'] or 0)/(row['value'] or 1) if metric!='tps' else 0
            segments.append(dict(width=fraction,color=color,unknown=key=='미확인',tip=tr(NAMES.get(key,key))+'\n'+value_text(c['value'],metric)+'\n'+tr('내부 구성비')+f' {fraction:.1%}'+(' ('+tr('확인분')+')' if row['partial'] else '')+'\n'+', '.join(c.get('names',[]))))
        group=row['group'];split=bool(data.get('breakdowns'))
        label=' / '.join(tr(v) for k,v in row['filters'].items() if k!='model') if split else tr(NAMES.get(row['label'],row['label'])) if data['dimension']!='project' else row['label']
        return dict(key=row['key'],label=label,group=tr(group),groupStart=bool(split and group!=previous_group),
                         value=value_text(row['value'],metric),missing=row['value'] is None,partial=row['partial'] and row['value'] is not None,
                         fraction=(1 if ratio else (row['value'] or 0)/maximum) if row['value'] else 0,
                         share=f"{row['value']/total['value']:.1%}" if row['value'] is not None and total['value'] else '—',
                         coverage=f"{row['known']:,} / {row['total']:,}",segments=segments,
                         color=theme.model_color(row['filters']['model']) if data['dimension']=='model' else 'accent',
                         tip=tr(TPS) if metric=='tps' else f"{row['value'] if row['value'] is not None else '—'}\n{row['known']:,} / {row['total']:,}")
