"""Metric comparison presentation, backed by worker projections."""
from copy import deepcopy
from datetime import datetime, timedelta
from PySide6.QtCore import Slot, QTimer
from .presentation import Node
from .i18n import tr
from .theme import shared_theme

DIMENSIONS = [('프로젝트','project'),('모델','model'),('작업 종류','source')]
BREAKDOWNS = [('요청 모드','service_tier'),('추론 설정','effort')]
METRICS = [('호출 수','calls'),('구독 가치 환산액','cost'),('입력 토큰','input'),('출력 토큰','output'),('추론 토큰','reasoning'),('호출당 TPS (평균)','tps')]
NAMES={'user':'유저 에이전트','subagent':'하위 에이전트','unknown':'미확인'}
TPS='각 호출의 출력 토큰을 전체 응답시간(초)으로 나눈 값의 산술평균입니다. 출력에는 추론이, 시간에는 대기와 통신이 포함됩니다. 유효한 완료 호출만 동일 가중으로 평균합니다.'


def value_text(value, metric):
    if value is None:return '—'
    if metric=='cost':return f'${value:,.2f}'
    if metric=='tps':return f'{value:,.1f}'
    if metric=='calls':return f'{round(value):,}'
    if value>=1e9:return f'{value/1e9:.2f}B'
    return f'{value/1e6:.2f}M' if value>=1e6 else f'{value/1000:.1f}k' if value>=1000 else f'{value:,}'


class UsagePanel(Node):
    kind='usageDashboard'

    def __init__(self, dashboard):
        super().__init__();self.dashboard=dashboard;self.data={}
        self.options=dict(period='all',start='',end='',filters={},archived=True,dimension='project',metric='calls',
                          mode='amount',search='',sort='desc',page=0,size=8,selected=[],views={},filtersOpen=False,breakdowns=[])
        self.search_timer=QTimer(self);self.search_timer.setSingleShot(True);self.search_timer.setInterval(160)
        self.search_timer.timeout.connect(self.refresh)
        shared_theme().changed.connect(self.restyle)
        self.put(options=deepcopy(self.options),
            dimensions=[dict(text=tr(t),value=v) for t,v in DIMENSIONS],metrics=[dict(text=tr(t),value=v) for t,v in METRICS],
            rows=[],legend=[],detail=[],scope=tr('전체 기록'),pageLabel='',countLabel='',title='',maximum=1,tpsTip='',metricLabel='')

    def restyle(self):
        if self.data:self.apply(self.data)

    def capture(self):return deepcopy(self.options)
    def restore(self,value):
        self.options.update(deepcopy({k:v for k,v in value.items() if k in self.options}))
        o=self.options;legacy=o['dimension']
        if legacy in dict((v,t) for t,v in BREAKDOWNS):
            o['dimension']='model';o['breakdowns']=[legacy];o['page']=0
        o['breakdowns']=[key for _,key in BREAKDOWNS if key in o['breakdowns']]
        self.put(options=deepcopy(o))

    def refresh(self):
        self.dashboard.render();self.dashboard.save_preferences()

    @Slot(str,str)
    def change(self,key,value):
        o=self.options
        if key=='filtersOpen':o[key]=not o[key];self.put(options=deepcopy(o));return
        if key=='search':
            o['search']=value;o['page']=0;self.put(options=deepcopy(o));self.search_timer.start();return
        if key=='page':o['page']=max(0,o['page']+int(value))
        elif key=='size':
            size=max(2,min(12,int(value)))
            if size==o['size']:return
            o['size']=size
        elif key=='dimension':
            if value not in dict((v,t) for t,v in DIMENSIONS):return
            o['views'][o['dimension']]={k:o[k] for k in ('search','sort','page')}
            o['dimension']=value;o.update(o['views'].get(value,dict(search='',sort='desc',page=0)))
        elif key.startswith('breakdown:'):
            field=key.split(':',1)[1]
            if field not in dict((v,t) for t,v in BREAKDOWNS):return
            selected=set(o['breakdowns'])
            if value=='true':selected.add(field)
            else:selected.discard(field)
            o['breakdowns']=[key for _,key in BREAKDOWNS if key in selected];o['page']=0
        elif key=='archived':o['archived']=value=='true';o['page']=0
        elif key.startswith('filter:'):
            self.dashboard.push_state();field=key.split(':',1)[1]
            if value:o['filters'][field]=value
            else:o['filters'].pop(field,None)
            o['selected']=[];o['page']=0
        elif key in ('period','start','end','metric','mode','sort'):
            if key in ('period','start','end'):self.dashboard.push_state()
            o[key]=value;o['page']=0
        else:return
        self.search_timer.stop();self.put(options=deepcopy(o));self.refresh()

    @Slot()
    def back(self):self.dashboard.go_back()

    @Slot()
    def reset(self):
        self.dashboard.push_state();self.options.update(filters={},selected=[],dimension='project',breakdowns=[],page=0,search='');self.put(options=deepcopy(self.options));self.refresh()

    @Slot(str)
    def select(self,key):
        row=next((row for row in self.data.get('rows',[]) if row['key']==key),None)
        if row is None:return
        self.dashboard.push_state();o=self.options;dimension=o['dimension'];o['filters'].update(row['filters'])
        o['selected']=[value for pair in row['filters'].items() for value in pair];o['dimension']='source' if dimension=='model' else 'model'
        if o['dimension'] in o['filters']:o['dimension']=next((d for _,d in DIMENSIONS if d not in o['filters']),'model')
        o.update(search='',page=0);self.put(options=deepcopy(o));self.refresh()

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
            try:
                start=datetime.fromisoformat(o['start']).timestamp();end=min(end,(datetime.fromisoformat(o['end'])+timedelta(days=1)).timestamp())
            except ValueError:start=end
        filters=dict(o['filters'])
        if filters.get('source')=='미확인':filters['source']='unknown'
        return dict(page=1,category_usage=True,presentation=True,include_whole_history=False,
                    start=start,end=end,now=now,period=period,archived=o['archived'],
                    loading=bool(self.dashboard.snapshot.get('index',{}).get('loading')),**filters,
                    usage={k:o[k] for k in ('dimension','metric','mode','search','sort','page','size','selected','breakdowns')})

    def apply(self,data):
        self.data=data;o=self.options;theme=shared_theme()
        o['page']=data['page']
        metric=data['metric'];ratio=o['mode']=='ratio' and metric!='tps';total=data['summary'];maximum=max(1,data['maximum']*1.05)
        legends={};rows=[];previous_group=None
        for row in data['rows']:
            segments=[]
            for c in row['components']:
                key=c['key'];color='unknown' if key=='미확인' else 'muted' if key=='기타' else theme.model_color(key) if data['cross']=='model' else 'accent' if key=='user' else 'reasoning'
                legends[key]=dict(label=tr(NAMES.get(key,key)),color=color)
                fraction=(c['value'] or 0)/(row['value'] or 1) if metric!='tps' else 0
                segments.append(dict(width=fraction,color=color,unknown=key=='미확인',tip=tr(NAMES.get(key,key))+'\n'+value_text(c['value'],metric)+'\n'+tr('내부 구성비')+f' {fraction:.1%}'+(' ('+tr('확인분')+')' if row['partial'] else '')+'\n'+', '.join(c.get('names',[]))))
            group=row['group'];split=bool(data.get('breakdowns'))
            label=' / '.join(tr(v) for k,v in row['filters'].items() if k!='model') if split else tr(NAMES.get(row['label'],row['label'])) if data['dimension']!='project' else row['label']
            rows.append(dict(key=row['key'],label=label,group=tr(group),groupStart=bool(split and group!=previous_group),
                             value=value_text(row['value'],metric),missing=row['value'] is None,partial=row['partial'] and row['value'] is not None,
                             fraction=(1 if ratio else (row['value'] or 0)/maximum) if row['value'] else 0,
                             share=f"{row['value']/total['value']:.1%}" if row['value'] is not None and total['value'] else '—',
                             coverage=f"{row['known']:,} / {row['total']:,}",segments=segments,
                             color=theme.model_color(row['filters']['model']) if data['dimension']=='model' else 'accent',
                             tip=tr(TPS) if metric=='tps' else f"{row['value'] if row['value'] is not None else '—'}\n{row['known']:,} / {row['total']:,}"))
            previous_group=group
        choices=[];labels=self.dashboard.snapshot.get('filter_choices',{}).get('project_labels',{})
        for caption,key in [('수집 위치','home'),*DIMENSIONS,*reversed(BREAKDOWNS)]:
            values=list(self.dashboard.filter_values.get(key,[]))
            selected=o['filters'].get(key)
            if selected and selected not in values:values.append(selected)
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
        periods={'all':'전체 기록','today':'오늘','7d':'최근 7일','30d':'최근 30일','custom':o['start']+' ~ '+o['end']}
        scope=tr(periods[o['period']])+' / '+(' / '.join(labels.get(v,tr(NAMES.get(v,v))) for v in o['filters'].values()) or tr('모든 관측 기록'))
        self.put(options=deepcopy(o),rows=rows,legend=[] if metric=='tps' else list(legends.values()),choices=choices,detail=detail,selectedLabel=selected_label,
                 scope=scope+(' / '+tr('보관 제외') if not o['archived'] else ''),title=tr(dict((v,t) for t,v in DIMENSIONS)[data['dimension']])+tr('별 ')+tr(dict((v,t) for t,v in METRICS)[metric]),
                 metric=metric,tpsTip=tr(TPS),metricLabel=tr(dict((v,t) for t,v in METRICS)[metric]),
                 shareLabel=tr('내부 구성비' if item else '전체 비중')+(' ('+tr('확인분')+')' if total['partial'] else ''),
                 middle='50%' if ratio else value_text(maximum/2,metric),upper='100%' if ratio else value_text(maximum,metric),
                 pageLabel=f"{data['page']+1} / {data['page_count']}",
                 countLabel=f"{min(data['start_index']+1,data['count'])}–{data['end_index']} / {data['count']}",
                 previous=data['page']>0,next=data['page']+1<data['page_count'])
