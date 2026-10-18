"""Measured cycle totals displayed below the existing overview time series."""
from datetime import datetime

from PySide6.QtCore import Qt, Signal

from .i18n import tr
from .pricing import usd
from .quota_view import clock
from .table_model import Table


def elapsed_text(seconds):
    seconds=max(0,seconds)
    if seconds<60:return f'{seconds:.3g} s'
    if seconds<3600:return f'{seconds/60:,.1f} min'
    return f'{seconds/3600:,.1f} hr'


class CycleTable(Table):
    cycle_activated=Signal(float)

    def __init__(self):
        super().__init__(headers=['주기','주기 소모량','주간 예상 가치','실측 시간'])
        self.put(inline=True,linkedHover=True,highlightedRow=-1,rowHeight=44,widths=[140,95,120,80],
                 columnStretchWeights=[2,1,1,1])
        self.setMinimumHeight(114)
        self.setAccessibleName('주기별 실측 시간과 소모량 및 주간 예상 가치 / Enter로 상세 열기')
        self.cellClicked.connect(self.activate_cycle);self.cellActivated.connect(self.activate_cycle)

    def set_periods(self,periods):
        fields=('start','end','current','partial','delta','value','calls','priced_calls')
        rows=[dict({key:period[key] for key in fields},id=period['start'])
              for period in sorted(periods,key=lambda period:period['start'])]
        self.set_rows(rows,self.format_row)
        self.setFixedHeight(max(114,70+self._state['rowHeight']*len(rows)))

    def activate_cycle(self,index,column=0):
        if 0<=index<self.rowCount():self.cycle_activated.emit(self.model().rows[index]['start'])

    @staticmethod
    def format_row(row,column,role):
        if role==Qt.TextAlignmentRole:return (Qt.AlignLeft if column==0 else Qt.AlignRight)|Qt.AlignVCenter
        status=tr('진행 중') if row['current'] else tr('부분 관측') if row['partial'] else ''
        if role==Qt.ToolTipRole:
            if column==0:
                states=[tr(text) for text,enabled in (('진행 중',row['current']),('부분 관측',row['partial'])) if enabled]
                return clock(row['start'],True)+' → '+clock(row['end'],True)+('\n'+' / '.join(states) if states else '')
            if column==3:
                return tr('첫 실측부터 마지막 실측까지의 경과 시간입니다. 수집 공백을 포함합니다.')+f"\n{max(0,row['end']-row['start']):g} s"
            if column==1:return tr('해당 주기에서 관측한 할당량 소모율입니다.')
            return (tr('확인된 비용의 소계')+f" / {row['priced_calls']:,}/{row['calls']:,}"+tr('호출')
                    +'\n'+tr('선택 주기의 사용 기록으로 계산한 주간 예상 가치')
                    if row['priced_calls']<row['calls'] else tr('선택 주기의 사용 기록으로 계산한 주간 예상 가치'))
        first,last=datetime.fromtimestamp(row['start']),datetime.fromtimestamp(row['end'])
        label=first.strftime('%m/%d %H:%M') if first.date()==last.date() else first.strftime('%m/%d')+' → '+last.strftime('%m/%d')
        if status:label+='\n'+status
        return [label,f"{row['delta']:g}%p",usd(row['value']),elapsed_text(row['end']-row['start'])][column]
