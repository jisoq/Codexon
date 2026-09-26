"""Cooperative relay shutdown shared by updates and application exit."""
import json
import re
import time
from .observer_control import atomic_write
from .observer_state import ProcessLock


class ProxyDrain:
    def __init__(self, manager, target, *, allowed=lambda:None, publish=lambda *a,**k:None,
                 before_drain=lambda source:None, force_requested=lambda:False, clock=time.monotonic, sleep=time.sleep):
        self.manager,self.target=manager,target
        self.allowed,self.publish,self.before_drain=allowed,publish,before_drain
        self.clock,self.sleep=clock,sleep
        self.force_requested=force_requested

    def run(self, source):
        m=self.manager
        health=m.health(timeout=3)
        # Unclassified work cannot be retired safely. Keep serving while the
        # client finishes/closes it instead of draining away every new ingress.
        # This also permits migration from the first lifecycle build, which did
        # not yet recognize Codex's metadata and rate-limit notifications.
        while health and (health.get('websocket_states') or {}).get('unknown') and not self.force_requested():
            self.allowed()
            if health['instance']!=source['instance']:raise RuntimeError('다른 프록시 인스턴스를 보존합니다.')
            self.publish('waiting',message='진행 여부를 판정할 수 없는 기존 연결의 종료 대기 · 통신 유지 중')
            self.sleep(1)
            health=m.health(timeout=3)
        if health:
            if health['instance']!=source['instance']:raise RuntimeError('다른 프록시 인스턴스를 보존합니다.')
            control_id=health.get('control_id','')
            import re
            if not re.fullmatch('[0-9a-f]{32}',control_id):
                raise RuntimeError('실행 중인 구버전에 안전 종료 제어가 없습니다. 기존 응답을 보존했습니다.')
            self.before_drain(source)
            self.publish('waiting',message='진행 중 응답 및 캐시 작업 정산 대기')
            if len(source.get('processes',[]))>1 and not self.target.cache:
                # Legacy supervisor observes the existing off phase before its
                # child exits, so it drains rather than treating exit as a crash.
                # Keep enabled and the user's route intact for the replacement.
                with ProcessLock(m.control_lock,timeout=5):
                    self.allowed()
                    state=m.state();state.update(phase='off',replacement_instance=source['instance'])
                    m.write_state(state)
            atomic_write(m.directory/('proxy-control-'+control_id+'.json'),
                         json.dumps(dict(action='drain',id=control_id)).encode())
        # No response deadline. Only after work has settled does the 30-second
        # process/port/ownership-lock exit deadline begin.
        exit_deadline=None
        force_sent=False
        while True:
            self.allowed()
            if self.target.stopped(source):return
            current=m.health(timeout=1)
            if current and current['instance']!=source['instance']:
                raise RuntimeError('종료 확인 중 다른 인스턴스가 발견되었습니다.')
            if self.force_requested() and not force_sent and current:
                if not current.get('supports_force_shutdown'):
                    raise RuntimeError('강제 종료를 지원하려면 연결 구성요소를 업데이트하세요.')
                captured=self.target.capture(current)
                control=current.get('control_id','')
                if captured['instance']!=source['instance'] or control!=source.get('control_id'):
                    raise RuntimeError('종료 확인 중 다른 인스턴스가 발견되었습니다.')
                atomic_write(m.directory/('proxy-control-'+control+'.json'),
                             json.dumps(dict(action='force_shutdown',id=control)).encode())
                force_sent=True;exit_deadline=self.clock()+30
            states=(current or {}).get('websocket_states')
            execution=(current or {}).get('cache_execution') or {}
            settled=self.target.exited(source) or (current is None and m.health_state=='refused') or (
                current and current.get('draining') and states is not None
                and not any(states.get(k,0) for k in ('responding','unknown','connecting','reserved'))
                and not current.get('http_connections') and execution.get('settled',not self.target.cache))
            if settled and exit_deadline is None:exit_deadline=self.clock()+30
            if exit_deadline is not None:
                self.publish('stopping',message='기존 프로세스·포트·실행 잠금 해제 확인 중')
                if self.clock()>=exit_deadline:raise RuntimeError('기존 프로세스 종료 확인 시간 초과 · 중복 기동하지 않았습니다.')
            else:self.publish('waiting',message='진행 중 응답 및 캐시 작업 정산 대기')
            self.sleep(1)
