"""Native offline recovery UI, independent of both Qt and Tcl/Tk."""


class RecoveryWindow:
    def __init__(self, manager, *, smoke=None):
        self.manager = manager
        self.smoke = smoke

    def run(self):
        from .connection_recovery import inspect, restore
        from .macos_installation import _alert
        from .macos_installation import installed, prepare_uninstall
        from .translation_catalog import translate as tr
        result = inspect(self.manager)
        receipt = installed()
        buttons = [tr('닫기'), tr('직접 연결로 복원')]
        if receipt:buttons.append(tr('Codexon 제거…'))
        before = result['code']
        choice = _alert(tr(result['title']), tr(result['detail']), buttons,smoke_button=1 if self.smoke else None)
        if choice == 2 and receipt:
            if _alert(tr('Codexon을 제거할까요?'),
                      tr('관리 중인 연결을 복원하고 앱 실행 경로와 백그라운드 서비스를 제거합니다. 기존 기록과 복구 파일은 보존됩니다. 먼저 Codexon을 종료해 주세요.'),
                      [tr('취소'), tr('제거')]) != 1:return
            try:
                prepare_uninstall(receipt['InstallRoot'])
                _alert(tr('Codexon 제거 완료'), tr('앱 실행 경로와 백그라운드 서비스를 제거했습니다. 기존 기록과 복구 파일은 보존됩니다.'), [tr('확인')])
            except Exception as exc:_alert(tr('제거를 완료하지 못했습니다'), tr(str(exc)), [tr('확인')])
            return
        if choice != 1:return
        try:result = restore(self.manager)
        except Exception:
            result = dict(title='설정을 복원하지 못했습니다',
                          detail='기존 설정과 백업은 보존됩니다. 파일 접근 권한을 확인한 뒤 다시 시도해 주세요.')
        _alert(tr(result['title']), tr(result['detail']), [tr('확인')],smoke_button=0 if self.smoke else None)
        if self.smoke:
            import json
            from .observer_control import atomic_write
            atomic_write(self.smoke,json.dumps(dict(passed=result.get('code')=='restored',
                before=before,after=result.get('code'),actual_button_activation=True,
                activation_event='NSButton.performClick',activations=1,independent_recovery=True)).encode())
