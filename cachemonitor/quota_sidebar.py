"""Sidebar account allowance and read-only reset-credit details."""
from decimal import Decimal
import time

from PySide6.QtCore import QDateTime, QRectF, Qt, QTimeZone
from PySide6.QtGui import QColor, QFont, QFontMetricsF

from .banked_resets import reset_credit_display
from .charts import Plot
from .i18n import formatted, tr
from .presentation import Button, Column, Group, Row, Scroll, Text
from .quota import quota_payload_display
from .quick_runtime import Dialog
from .theme import shared_theme


def reset_time(stamp, zone):
    return QDateTime.fromSecsSinceEpoch(int(stamp),zone) if stamp else None


def reset_zone_label(zone):
    name=bytes(zone.id()).decode('utf-8')
    if name=='Asia/Seoul':return 'KST (UTC+09:00)'
    if name in ('UTC','Etc/UTC','Etc/GMT'):return 'UTC'
    return name.replace('_',' ')


def format_credit_balance(balance):
    text=format(Decimal(balance),',f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _label(text='',size=12,color='ink',bold=False):
    node=Text(text);node.put(fontSize=size,color=color,bold=bold)
    return node


def _duration(seconds):
    days=int(seconds//86400);hours=int(seconds%86400//3600);minutes=int(seconds%3600//60)
    return (formatted('{days}일 {hours}시간',days=days,hours=hours) if days else
            formatted('{hours}시간 {minutes}분',hours=hours,minutes=minutes) if hours else
            formatted('{minutes}분',minutes=minutes) if minutes else tr('1분 미만'))


class QuotaBar(Plot):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(6);self.setFocusPolicy(Qt.NoFocus)
        self.empty_text='';self.remaining=None
        shared_theme().changed.connect(self.update)

    def set_value(self,value):
        self.remaining=value;self.update()

    def paint(self,painter):
        palette=shared_theme().palette
        p=self.base()
        p.fillRect(self.rect(),QColor(palette['secondary']))
        p.fillRect(QRectF(0,0,self.width(),6),QColor(palette['border']))
        if self.remaining is not None:
            p.fillRect(QRectF(0,0,self.width()*self.remaining/100,6),QColor(palette['accent']))


class QuotaSidebar(Group):
    """A compact view of one home; the dashboard owns account selection."""
    def __init__(self):
        super().__init__()
        self.setObjectName('quota-sidebar');self.put(background='secondary',radius=10)
        self.quota=None;self.payload={};self.issue='';self.reset_dialog=None;self.reset_items=[]
        self.lookup_failed=False;self.failure_note=''
        self._reset_rows=[];self._reset_note='';self._dialog_signature=None
        body=Column(self);body.setContentsMargins(12,12,12,12);body.setSpacing(7)
        heading=Row();heading.setSpacing(4)
        self.title=_label('남은 사용량',12,bold=True);heading.addWidget(self.title,1)
        self.plan=_label('',11,'muted');self.plan.setMaximumWidth(52);heading.addWidget(self.plan)
        body.addLayout(heading)
        self.current={}
        for mode,title,size in (('five_hour','5시간',22),('weekly','주간',27)):
            row=Row();row.setSpacing(4);row.addWidget(_label(title,12,'muted'),1)
            value=_label('—',size,bold=True);value.put(noElide=True);row.addWidget(value)
            body.addLayout(row)
            bar=QuotaBar();body.addWidget(bar)
            evidence=_label('',11,'muted');evidence.put(wrap=True);body.addWidget(evidence)
            self.current[mode]=dict(value=value,bar=bar,evidence=evidence)
        self.five=self.current['five_hour']['value'];self.weekly=self.current['weekly']['value']
        line=Group();line.setFixedHeight(1);line.put(background='border');body.addWidget(line)
        credit_row=Row();credit_row.setSpacing(4)
        self.credit_label=_label('크레딧',12,'muted');credit_row.addWidget(self.credit_label)
        self.credit_balance=_label('—',20,bold=True)
        self.credit_balance.put(noElide=True,wrap=True,alignment=int(Qt.AlignRight))
        credit_row.addWidget(self.credit_balance,1);body.addLayout(credit_row)
        self.reset_button=Button();self.reset_button.put(role='quiet',fontSize=12,metricReset=True)
        self.reset_button.setFixedHeight(26)
        self.reset_button.setAccessibleName('초기화권 만료일과 지급 정보 보기')
        self.reset_button.clicked.connect(self.show_details);body.addWidget(self.reset_button)
        self.expiry=_label('',10,'muted');body.addWidget(self.expiry)
        self.reset_note=self.expiry
        self.status=_label('',10,'muted');body.addWidget(self.status)
        self.refresh_status()

    def receive(self,value):
        self.payload=value
        self.quota=value.get('quota');self.issue=value.get('issue','')
        from .app_errors import detect
        facts=(value.get('quota_faults') or {}).get('lookup') or {}
        self.lookup_failed=bool(facts.get('lookup_failed'))
        fault=detect('quota',facts,time.time())
        self.failure_note=fault.explanation+'\n'+fault.guidance if fault else ''
        self.refresh_status()

    def refresh_status(self):
        quota=self.quota or {};zone=QTimeZone.systemTimeZone()
        now=time.time()
        observed=quota.get('observed_at',0)
        fresh=bool(observed and 0<=now-observed<90 and quota.get('source')=='live')
        plan=quota.get('plan_type')
        plan_text={'free':'Free','plus':'Plus','pro':'Pro','team':'Team','business':'Business',
                   'enterprise':'Enterprise','edu':'Edu'}.get(plan,'')
        self.plan.setText(plan_text);self.plan.setToolTip(plan_text);self.plan.setVisible(bool(plan_text))
        for mode,card in self.current.items():
            display=quota_payload_display(self.payload,mode,now)
            remaining=display['remaining']
            card['value'].setText(f'{remaining:.1f}%' if remaining is not None else
                                  '∞' if display['state']=='제한 없음' else '—')
            card['value'].setToolTip(display['tooltip'])
            card['bar'].set_value(remaining)
            card['bar'].setVisible(mode=='weekly' and remaining is not None)
            reset=quota.get('windows',{}).get(mode,{}).get('resets_at')
            stamp=reset_time(reset,zone)
            if stamp and remaining is not None and reset>now:
                card['evidence'].setText(formatted('{duration} 후 초기화\n{date}',
                    duration=_duration(reset-now),date=stamp.toString('MM/dd HH:mm')))
                card['evidence'].setToolTip(formatted('정기 초기화: {date} {zone}',
                    date=stamp.toString('yyyy/MM/dd HH:mm'),zone=reset_zone_label(zone)))
            elif remaining is None and display['state']!='제한 없음':
                card['evidence'].setText(display['state']);card['evidence'].setToolTip(display['tooltip'])
            else:
                card['evidence'].setText('');card['evidence'].setToolTip('')
            # Five-hour details remain available without lengthening the common
            # unlimited-plan card. Finite windows retain their own reset time.
            card['evidence'].setVisible(bool(card['evidence'].text()))
        credits=quota.get('credits') or {};balance=credits.get('balance')
        credit_text=('∞' if fresh and credits.get('unlimited') else
                     format_credit_balance(balance) if fresh and balance is not None else '—')
        self.credit_balance.setText(credit_text)
        credit_note=('제한 없음' if credits.get('unlimited') else
                     '보유 크레딧 없음' if balance is not None and Decimal(balance)==0 else
                     '만료일 정보 미제공') if fresh and (balance is not None or credits.get('unlimited')) else (
                     '조회 갱신 대기' if credits else '크레딧 정보 미제공')
        self.credit_balance.setToolTip(formatted('크레딧 잔액 {balance}\n{note}',balance=credit_text,note=tr(credit_note)))
        # Preserve fractional balances as exact text, shrinking only the number.
        # Very long balances wrap instead of becoming a different rounded value.
        font=QFont(shared_theme().family);font.setPixelSize(12)
        available=max(60,144-QFontMetricsF(font).horizontalAdvance(tr('크레딧'))-4)
        font.setBold(True);size=20
        while size>11:
            font.setPixelSize(size)
            if QFontMetricsF(font).horizontalAdvance(credit_text)<=available:break
            size-=1
        self.credit_balance.put(fontSize=size)
        resets=reset_credit_display(self.quota,now)
        count=((quota.get('reset_credits') or {}).get('available_count'))
        count_text=formatted('{count}개',count=f'{count:,}') if fresh and count is not None else '—'
        self.reset_button.setText(count_text)
        rows=((quota.get('reset_credits') or {}).get('credits') or []) if fresh else []
        self._reset_rows=sorted(rows,key=lambda row:row.get('expires_at') or float('inf'))
        self._reset_note=resets['note'].replace(chr(183),' / ')
        available_rows=[row for row in rows if row.get('status')=='available']
        deadlines=[row['expires_at'] for row in available_rows if row.get('expires_at')]
        if fresh and count==0:summary='보유 초기화권 없음'
        elif fresh and deadlines and min(deadlines)>now:
            template='가장 빠른 만료 {date}' if len(deadlines)==count else '확인된 만료 {date}'
            summary=formatted(template,date=reset_time(min(deadlines),zone).toString('MM/dd HH:mm'))
        elif fresh and deadlines:summary='만료 시각 경과, 갱신 대기'
        elif not fresh:summary='조회 갱신 대기' if observed else '정보 미제공'
        else:summary='만료일 정보 미제공'
        if fresh and deadlines and min(deadlines)>now:
            # The same selected zone governs the compact date and full tooltip.
            template='가장 빠른 만료 {date}' if len(deadlines)==count else '확인된 만료 {date}'
            note=formatted(template,date=reset_time(min(deadlines),zone).toString('yyyy/MM/dd HH:mm'))
            remainder=self._reset_note.splitlines()[1:]
            self._reset_note='\n'.join([str(note),*remainder])
        self.expiry.setText(summary);self.expiry.setToolTip(self._reset_note)
        self.reset_button.setToolTip(self._reset_note)
        age=now-observed if observed else None
        status=('최신 잔여량 미확인' if self.lookup_failed else
                '조회 갱신 대기' if self.issue or age is None or age<0 else
                '갱신 지연' if age>=90 else '방금 갱신' if age<60 else
                formatted('{minutes}분 전 갱신',minutes=int(age//60)))
        self.status.setText(status)
        stamp=reset_time(observed,zone)
        checked=formatted('마지막 확인 {date}',date=stamp.toString('yyyy/MM/dd HH:mm')) if stamp else ''
        self.status.setToolTip('\n'.join(part for part in (self.failure_note or self.issue,checked) if part).replace(chr(183),' / '))
        if self.reset_dialog:
            signature=(repr(self._reset_rows),self._reset_note,str(count_text),bytes(zone.id()))
            if signature!=self._dialog_signature:self._render_details(signature,count_text,zone)

    def show_details(self):
        if self.reset_dialog and self.reset_dialog.host:
            self.reset_dialog.host.raise_();self.reset_dialog.host.activateWindow();return
        dialog=Dialog(self);self.reset_dialog=dialog
        dialog.setWindowTitle(tr('초기화권 상세'));dialog.resize(430,410)
        outer=Column(dialog);outer.setContentsMargins(24,22,24,22);outer.setSpacing(14)
        self.details_title=_label('초기화권',22,bold=True);outer.addWidget(self.details_title)
        self.details_zone=_label('',12,'muted');self.details_zone.put(wrap=True);outer.addWidget(self.details_zone)
        self.details_scroll=Scroll();self.details_scroll.put(fillViewport=True);outer.addWidget(self.details_scroll,1)
        self.details_listing=Group();self.details_rows=Column(self.details_listing);self.details_rows.setSpacing(12)
        self.details_scroll.setWidget(self.details_listing)
        self.details_note=_label('',12,'muted');self.details_note.put(wrap=True);self.details_rows.addWidget(self.details_note)
        self.reset_items=[];self._dialog_signature=None
        close=Button('닫기');close.clicked.connect(dialog.reject);outer.addWidget(close)
        self.details_close=close
        dialog.finished.connect(self._details_closed)
        self.refresh_status();dialog.open()

    def _details_closed(self,*_):
        dialog=self.reset_dialog;self.reset_dialog=None;self.reset_items=[];self._dialog_signature=None
        if dialog:dialog.deleteLater()

    def _render_details(self,signature,count,zone):
        self._dialog_signature=signature
        self.details_title.setText(formatted('초기화권 {count}',count=count))
        self.details_note.setText(self._reset_note);self.details_note.setVisible(bool(self._reset_note))
        years=set()
        deadlines=[row['expires_at'] for row in self._reset_rows
                   if row.get('status')=='available' and row.get('expires_at')]
        earliest=min(deadlines) if deadlines else None
        server_count=((self.quota or {}).get('reset_credits') or {}).get('available_count')
        while len(self.reset_items)<len(self._reset_rows):
            card=Group();card.put(background='secondary',radius=6)
            column=Column(card);column.setContentsMargins(14,12,14,12);column.setSpacing(5)
            label=_label('',12,'muted');expiry=_label('',20,bold=True);expiry.put(noElide=True)
            granted=_label('',12,'muted');granted.put(wrap=True)
            status=_label('',12,'muted');status.put(wrap=True)
            description=_label('',12,'muted');description.put(wrap=True)
            for node in (label,expiry,granted,status,description):column.addWidget(node)
            self.details_rows.addWidget(card)
            self.reset_items.append(dict(card=card,label=label,expiry=expiry,granted=granted,status=status,description=description))
        for index,item in enumerate(self.reset_items):
            exists=index<len(self._reset_rows);item['card'].setVisible(exists)
            if not exists:continue
            row=self._reset_rows[index]
            expiry=reset_time(row.get('expires_at'),zone);granted=reset_time(row.get('granted_at'),zone)
            for stamp in (expiry,granted):
                if stamp:years.add(stamp.date().year())
            first=row.get('status')=='available' and expiry and row.get('expires_at')==earliest
            item['label'].setText(('가장 빠른 만료' if len(deadlines)==server_count else '확인된 만료') if first else '만료')
            item['expiry'].setText(expiry.toString('MM/dd HH:mm') if expiry else '정보 미제공')
            item['expiry'].setToolTip(expiry.toString('yyyy/MM/dd HH:mm tttt') if expiry else '만료일 정보 미제공')
            item['granted'].setText(formatted('지급 {date}',date=granted.toString('MM/dd HH:mm')) if granted else '지급일 정보 미제공')
            item['granted'].setToolTip(granted.toString('yyyy/MM/dd HH:mm tttt') if granted else '')
            state={'available':'사용 가능','redeemed':'사용 완료','consumed':'사용 완료','expired':'만료됨'}.get(row.get('status'),'상태 미제공')
            item['status'].setText(state)
            description='\n'.join(value for value in (row.get('title'),row.get('description')) if value).replace(chr(183),' / ')
            item['description'].setText(description);item['description'].setVisible(bool(description))
        zone_name=reset_zone_label(zone)
        self.details_zone.setText(formatted('{years}년, {zone}',years=', '.join(map(str,sorted(years))),zone=zone_name) if years else zone_name)
        self.details_zone.setToolTip(bytes(zone.id()).decode('utf-8'))
