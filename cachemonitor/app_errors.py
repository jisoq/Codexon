"""Six user-facing failures, classified from source facts rather than error text."""
from collections import deque
from dataclasses import dataclass, asdict
import time

TITLES = {
    'collection': '수집기 오류 발생', 'analysis': '분석 오류 발생',
    'connection': 'Codex 연결 오류 발생', 'observation': '모델 관측 오류 발생',
    'quota': '사용 한도 오류 발생', 'update': '업데이트 오류 발생',
}


@dataclass(frozen=True)
class Fault:
    kind: str
    code: str
    severity: str
    explanation: str
    guidance: str
    action: str
    page: str
    notify: bool = False
    tray: bool = True

    @property
    def title(self):
        if self.kind == 'collection' and self.code == 'stale':
            return '수집 결과 갱신 지연'
        return TITLES[self.kind]

    def export(self):
        return dict(asdict(self), title=self.title)


def detect(kind, value, now):
    """No classification by exception-message text; callers supply source facts."""
    if value.get('expected_stop') or value.get('disabled'):
        return None
    if kind == 'collection':
        if value.get('start_failed') or value.get('runtime_failed'):
            start = value.get('start_failed', False)
            exhausted = value.get('retry_exhausted', False)
            return Fault(kind, 'start' if start else 'runtime', 'error',
                '수집기를 시작하지 못해 새 사용 기록을 반영할 수 없습니다.' if start else
                '수집기에서 오류가 발생해 새 사용 기록을 반영할 수 없습니다.',
                '자동 재시작에도 실패했으므로 Codexon을 종료한 뒤 다시 켜주세요.' if exhausted else
                '수집기를 다시 시작하고 있으며, 정상 실행을 확인하면 기록을 자동으로 갱신합니다.',
                '수집 상태 확인', 'collection', notify=exhausted)
        if value.get('read_failed') or value.get('save_failed'):
            save = value.get('save_failed', False)
            return Fault(kind, 'save' if save else 'read', 'error' if save else 'warning',
                '사용 기록을 저장하지 못해 새 사용량을 기록할 수 없습니다.' if save else
                '일부 사용 기록을 읽지 못해 전체 사용량을 정확히 집계할 수 없습니다.',
                '저장 공간과 기록 폴더의 접근 권한을 확인한 뒤 다시 시도해주세요.' if save else
                '수집 상태에서 문제가 있는 기록을 확인한 뒤 다시 시도해주세요.',
                '수집 상태 확인', 'collection', notify=save)
        if value.get('status_failed'):
            return Fault(kind,'status','warning',
                '수집기의 실행 상태를 확인하는 중 오류가 발생해 정상 동작 여부를 확인하지 못했습니다.',
                '기존 기록은 유지되니 수집 상태를 다시 확인해주세요.',
                '수집 상태 확인','collection')
        if (not value.get('starting') and value.get('snapshot_at') is not None
                and now - value['snapshot_at'] > 30):
            return Fault(kind, 'stale', 'warning',
                '새 수집 결과가 30초 넘게 갱신되지 않아 최신 사용량을 확인할 수 없습니다.',
                '원인은 아직 확인하지 못했으며, 수집기의 실행 상태를 다시 확인하고 있습니다.',
                '수집 상태 확인', 'collection')
    elif kind == 'analysis':
        if value.get('fatal') or value.get('start_failed') or value.get('process_exited'):
            retrying = value.get('retrying', False)
            exhausted = value.get('retry_exhausted', False)
            return Fault(kind, 'process', 'warning' if retrying else 'error',
                '분석기를 실행하지 못해 사용 기록의 분석 결과를 갱신할 수 없습니다.',
                '분석기를 다시 시작하고 있으며, 정상 실행을 확인하면 결과를 갱신합니다.' if retrying else
                '자동 재시작에 실패했으므로 Codexon을 종료한 뒤 다시 켜주세요.' if exhausted else
                '분석기를 시작할 수 없으므로 Codexon을 종료한 뒤 다시 켜주세요.' if value.get('start_failed') else
                '분석기의 실행 상태를 확인하고 있습니다. 문제가 계속되면 Codexon을 다시 켜주세요.',
                '다시 시도', 'analysis', notify=exhausted)
        if (value.get('query_failed') and value.get('request_id') == value.get('current_request_id')):
            retained = value.get('retained', True)
            return Fault(kind, 'query', 'warning',
                '기록을 계산하는 중 오류가 발생해 분석 결과를 갱신하지 못했습니다.',
                '이전에 표시한 결과는 유지되니 잠시 후 다시 시도해주세요.' if retained else
                '현재 조건의 결과를 불러오지 못했으므로 잠시 후 다시 시도해주세요.',
                '다시 시도', 'analysis', tray=False)
    elif kind == 'connection':
        from .connection_recovery import assess
        if value.get('phase') == 'recovery_failed' or (value.get('incident') or {}).get('recovery_error'):
            return Fault(kind, 'restore', 'error',
                '프록시 연결 설정을 복원하지 못해 Codex 연결에 문제가 남아 있습니다.',
                '연결 복구를 실행한 뒤 작업을 마치고 Codex를 완전히 종료했다가 다시 열어주세요.',
                '연결 복구', 'integration', notify=True)
        if value.get('operation_failed'):
            return Fault(kind,'configuration','warning',
                'Codex 연결 설정을 처리하는 중 오류가 발생해 요청한 작업을 마치지 못했습니다.',
                '연결 설정에서 현재 상태를 확인한 뒤 다시 시도해주세요.',
                '연결 상태 확인','integration',tray=False)
        if value.get('protection_stopped'):
            return Fault(kind,'protection','warning',
                'Codex 연결 오류로 프록시를 멈추고 직접 연결 설정으로 복원했습니다.',
                '작업을 마친 뒤 Codex를 완전히 종료했다가 다시 열어주세요.',
                '연결 상태 확인','integration',notify=value.get('notification_confirmed',False))
        assessed = assess(dict(configured=False) | value)
        code = assessed['code']
        if code in ('refused', 'identity_mismatch', 'relay_failure'):
            text = {
                'refused': '설정된 프록시가 연결을 거부해 Codex 요청을 전달할 수 없습니다.',
                'identity_mismatch': '설정된 프록시와 응답한 서비스의 식별 정보가 달라 Codex 연결을 확인할 수 없습니다.',
                'relay_failure': '프록시 내부 처리 오류가 반복되어 Codex 요청을 정상적으로 전달하지 못하고 있습니다.',
            }[code]
            return Fault(kind, code, 'error', text,
                '연결 복구를 실행한 뒤 작업을 마치고 Codex를 완전히 종료했다가 다시 열어주세요.',
                '연결 복구', 'integration', notify=value.get('notification_confirmed', False))
    elif kind == 'observation':
        health = value.get('health') or value
        if value.get('protection_stopped'):
            return Fault(kind,'protection','warning',
                '모델 관측 기록의 저장 오류가 반복되어 프록시를 멈추고 직접 연결 설정으로 복원했습니다.',
                '작업을 마친 뒤 Codex를 완전히 종료했다가 다시 열어주세요. 관측을 다시 켜기 전에 저장 상태를 확인해주세요.',
                '관측 상태 확인','integration',notify=value.get('notification_confirmed',False))
        if value.get('read_failed'):
            code, text = 'read', '모델 관측 기록을 읽지 못해 기록된 요청 모델과 응답 모델을 비교할 수 없습니다.'
        elif health.get('storage_failure_streak', 0) >= 3 or health.get('observation_enabled') is False:
            code, text = 'save', '모델 관측 기록을 저장하지 못해 새 요청의 모델 비교 정보를 기록할 수 없습니다.'
        elif health.get('observation_dropped', 0) > value.get('previous_dropped', 0):
            code, text = 'gap', '모델 관측 기록 일부가 누락되어 해당 요청의 모델 비교 정보를 확인할 수 없습니다.'
        else:
            return None
        return Fault(kind, code, 'warning', text,
            'Codex 연결 설정에서 관측 상태를 확인해주세요. 이 오류만으로 Codex 통신 실패를 판단하지는 않습니다.',
            '관측 상태 확인', 'integration')
    elif kind == 'quota':
        if value.get('stale'):
            return None
        if value.get('uncertain'):
            return Fault(kind, 'uncertain', 'warning',
                '사용 한도 구간의 저장 결과를 확인하지 못해 변경 사항이 적용됐는지 알 수 없습니다.',
                '제외 내역을 확인한 뒤 다시 시도해주세요.', '제외 내역 확인', 'quota', tray=False)
        if value.get('ledger_failed'):
            return Fault(kind,'ledger','warning',
                '사용 한도 기록을 읽거나 갱신하는 중 오류가 발생해 이력 반영을 완료하지 못했습니다.',
                '기존 사용량 결과는 유지되며, 기록 상태를 다시 확인하고 있습니다.',
                '사용 한도 확인','quota')
        if value.get('calculation_failed'):
            saved = value.get('saved', False)
            return Fault(kind, 'calculation', 'warning',
                '구간 변경은 저장했지만 재집계 중 오류가 발생해 사용 한도 화면을 갱신하지 못했습니다.' if saved else
                '사용 한도를 계산하는 중 오류가 발생해 집계 결과를 갱신하지 못했습니다.',
                '저장된 내역은 유지되며, 계산이 완료되면 결과를 자동으로 갱신합니다.',
                '다시 시도', 'quota', tray=False)
        if value.get('storage_failed'):
            temporary=value.get('temporary',False)
            return Fault(kind, 'storage', 'warning' if temporary else 'error',
                '사용 한도 이력을 저장하지 못해 새 관측 기록을 보관할 수 없습니다.',
                '기록 저장을 다시 시도하고 있으며, 저장이 완료되면 한도 이력을 갱신합니다.' if temporary else
                '저장 공간과 기록 폴더의 접근 권한을 확인해주세요.',
                '사용 한도 확인', 'quota', notify=not temporary)
        if value.get('lookup_failed'):
            code = value.get('reason', 'lookup')
            text = {
                'account_missing': 'Codex 계정 정보가 없어 남은 사용 한도를 조회할 수 없습니다.',
                'timeout': '사용 한도 조회 시간이 초과되어 최신 잔여량을 확인하지 못했습니다.',
            }.get(code, '사용 한도를 조회하는 중 오류가 발생해 최신 잔여량을 확인하지 못했습니다.')
            guidance = ('Codex에 로그인한 뒤 사용 한도를 다시 확인해주세요.' if code == 'account_missing' else
                '자동으로 다시 조회하고 있으며, 정상 응답을 받으면 잔여량을 갱신합니다.')
            return Fault(kind, code, 'warning', text, guidance,
                '사용 한도 확인', 'quota', notify=code == 'account_missing')
    elif kind == 'update':
        if value.get('checking_failed'):
            return Fault(kind, 'check', 'warning',
                '업데이트 정보를 받아오지 못해 새 버전이 있는지 확인할 수 없습니다.',
                '잠시 후 업데이트를 다시 확인해주세요.', '다시 확인', 'about', tray=False)
        phase, proxy = value.get('phase'), value.get('proxy') or {}
        failed = phase in ('failed', 'interrupted') or (phase == 'partial' and proxy.get('phase') in ('failed', 'recovery_required'))
        if failed and not value.get('cancel_requested'):
            partial = value.get('app', {}).get('state') in ('verified', 'current')
            return Fault(kind, 'proxy' if partial else 'install', 'warning',
                '앱 설치는 완료했지만 프록시 업데이트에 실패해 연결 구성요소를 적용하지 못했습니다.' if partial else
                '업데이트를 적용하는 중 오류가 발생해 새 버전을 적용하지 못했습니다.',
                '앱 정보에서 업데이트를 다시 적용해주세요.', '업데이트 확인', 'about', notify=True)
    return None


def strongest(faults):
    visible = [fault for fault in faults if fault.tray]
    return next((fault for fault in visible if fault.severity == 'error'), next(iter(visible), None))


def exception_facts(error):
    """Small, privacy-safe metadata; never copy an arbitrary exception message."""
    return dict(error_type=type(error).__name__[:64],
                errno=getattr(error,'errno',None),winerror=getattr(error,'winerror',None),
                sqlite_code=getattr(error,'sqlite_errorcode',None))


class ErrorState:
    # A fixed number of source slots prevents home, file and request IDs from
    # growing the registry. Each category can retain several independent failures.
    SOURCES=frozenset(('collection.service','collection.source','collection.freshness',
        'analysis.process','analysis.query','quota.ledger','quota.analysis','quota.live','quota.edit','quota.calculation','quota.query',
        'connection.status','connection.protection','connection.operation','observation.read',
        'observation.health','observation.protection','update.status','update.operation','update.check'))

    def __init__(self,history=None,clock=time.time):
        self.clock=clock
        self.history=deque(maxlen=50) if history is None else history
        self.sources={};self.records={};self.revision=0

    def update(self,source,kind,facts,*,healthy=False,resolution='정상 동작 확인'):
        if source not in self.SOURCES:raise ValueError('Unknown error source')
        current=self.sources.get(source)
        fault=detect(kind,facts,self.clock())
        if fault is None:
            if healthy:self.clear(source,resolution=resolution)
            return None
        if current==fault:return None
        self.revision+=1
        self.sources[source]=fault
        record=self.records.get(source)
        if current is None or (current.kind,current.code)!=(fault.kind,fault.code):
            if record:record['resolution']='상태 변경'
            record=dict(kind='app_error',error_kind=kind,error_code=fault.code,
                source=source,title=fault.title,detail=fault.explanation+'\n'+fault.guidance,
                action=fault.action,page=fault.page,count=1,at=self.clock())
            self.history.appendleft(record);self.records[source]=record
        else:
            record.update(title=fault.title,detail=fault.explanation+'\n'+fault.guidance,
                action=fault.action,page=fault.page)
        already_notified=any(other.notify and (other.kind,other.code)==(fault.kind,fault.code)
            for key,other in self.sources.items() if key!=source)
        if fault.notify and not already_notified and (current is None or not current.notify or current.severity!=fault.severity):
            return fault
        return None

    def clear(self,source,*,resolution='정상 동작 확인'):
        if self.sources.pop(source,None) is not None:self.revision+=1
        record=self.records.pop(source,None)
        if record:record['resolution']=resolution

    def grouped(self):
        groups={}
        for fault in self.sources.values():
            previous=groups.get(fault.kind)
            if previous is None or (fault.severity=='error' and previous.severity!='error'):
                groups[fault.kind]=fault
        return groups
