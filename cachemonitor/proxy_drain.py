"""Cooperative relay shutdown shared by updates and application exit."""
import json
import re
import time
from .observer_control import atomic_write
from .observer_state import ProcessLock

class ProxyDrain:

    def __init__(self, manager, target, *, allowed=lambda: None, publish=lambda *a, **k: None, before_drain=lambda source: None, force_requested=lambda: False, clock=time.monotonic, sleep=time.sleep, purpose='update', allow_missing_flush=False):
        self.manager, self.target = (manager, target)
        self.allowed, self.publish, self.before_drain = (allowed, publish, before_drain)
        self.clock, self.sleep = (clock, sleep)
        self.force_requested = force_requested
        self.purpose = purpose
        self.allow_missing_flush = allow_missing_flush

    def run(self, source):
        m = self.manager
        drain_sent = False
        exit_deadline = None
        force_sent = False
        while True:
            self.allowed()
            if self.target.stopped(source):
                return
            if self.allow_missing_flush and self.target.released(source):
                return False
            current = m.health(timeout=1 if drain_sent else 3)
            if current and current['instance'] != source['instance']:
                raise RuntimeError('종료 확인 중 다른 인스턴스가 발견되었습니다.')
            force = self.force_requested()
            can_force = bool(current and current.get('supports_force_shutdown')
                and re.fullmatch('[0-9a-f]{32}', current.get('control_id') or '')
                and current.get('control_id') == source.get('control_id'))
            if force and current and not force_sent:
                if not can_force:
                    raise RuntimeError('강제 종료를 지원하려면 연결 구성요소를 업데이트하세요.')
                captured = self.target.capture(current)
                if captured['instance'] != source['instance'] or captured.get('control_id') != source.get('control_id'):
                    raise RuntimeError('종료 확인 중 다른 인스턴스가 발견되었습니다.')
            if current and (not drain_sent):
                states=current.get('websocket_states')
                unknown=(states or {}).get('unknown',0)
                if states is None:unknown=current.get('active_connections',0)
                responding=(states or {}).get('responding',0)+current.get('http_connections',0)
                if unknown and not force:
                    self.publish('waiting', required_action='close_client',unknown_connections=unknown,
                                 responding=responding, can_force=can_force,
                                 message=('연결 상태를 확인하지 못한 작업이 있습니다. 연결한 앱에서 작업을 마치거나 연결을 종료하세요.\n종료 준비를 취소하고 Codexon을 계속 사용할 수 있습니다.'
                                          if self.purpose == 'exit' else '연결한 앱을 정상 종료하세요. Codexon은 켜 두세요. 연결 종료를 확인하면 자동으로 계속합니다.'))
                    self.sleep(1)
                    continue
                if responding and not force:
                    self.publish('waiting',required_action=None,unknown_connections=0,responding=responding,
                                 can_force=can_force,
                                 message=('진행 중인 응답이 끝나기를 기다리고 있습니다.\n종료 준비를 취소하고 Codexon을 계속 사용할 수 있습니다.'
                                          if self.purpose == 'exit' else '진행 중인 응답이 끝나기를 기다리고 있습니다.\n새 작업은 업데이트 완료 후 시작하세요.'))
                    self.sleep(1)
                    continue
                control_id = current.get('control_id') or ''
                if not re.fullmatch('[0-9a-f]{32}', control_id):
                    raise RuntimeError('실행 중인 구버전에 안전 종료 제어가 없습니다. 기존 응답을 보존했습니다.')
                if control_id != source.get('control_id'):
                    raise RuntimeError('종료 확인 중 다른 인스턴스가 발견되었습니다.')
                self.before_drain(source)
                self.publish('switching',required_action=None,responding=0,unknown_connections=0,can_force=can_force,
                             message='연결을 마무리하고 기록을 저장하고 있습니다.')
                if len(source.get('processes', [])) > 1:
                    with ProcessLock(m.control_lock, timeout=5):
                        self.allowed()
                        state = m.state()
                        state.update(phase='off', replacement_instance=source['instance'])
                        m.write_state(state)
                atomic_write(m.directory / ('proxy-control-' + control_id + '.json'), json.dumps(dict(action='drain', id=control_id)).encode())
                drain_sent = True
            if force and not force_sent and current:
                control = current['control_id']
                atomic_write(m.directory / ('proxy-control-' + control + '.json'), json.dumps(dict(action='force_shutdown', id=control)).encode())
                force_sent = True
                exit_deadline = self.clock() + 30
            states = (current or {}).get('websocket_states')
            settled = self.target.exited(source) or (current is None and m.health_state == 'refused') or (current and current.get('draining') and (states is not None) and (not any((states.get(k, 0) for k in ('responding', 'unknown', 'connecting', 'reserved')))) and (not current.get('http_connections')))
            if settled and exit_deadline is None:
                exit_deadline = self.clock() + 30
            if exit_deadline is not None:
                self.publish('stopping', can_force=can_force, message='기존 프록시 종료와 포트 해제를 확인하고 있습니다.')
                if self.clock() >= exit_deadline:
                    raise RuntimeError('기존 프로세스 종료 확인 시간 초과 / 중복 기동하지 않았습니다.')
            else:
                unknown=(states or {}).get('unknown',0)
                self.publish('waiting' if unknown else 'switching',required_action='close_client' if unknown else None,
                             unknown_connections=unknown,
                             responding=(states or {}).get('responding',0)+(current or {}).get('http_connections',0),
                             can_force=can_force,
                             message='마지막 응답 전달과 기록 저장이 끝나기를 기다리고 있습니다.')
            self.sleep(1)
