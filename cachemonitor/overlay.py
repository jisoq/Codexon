"""Native overlay coordination, separate from Codex and the data collector."""
import os
import time
import math
from PySide6.QtCore import Qt, QTimer, QObject, Signal, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QAction, QActionGroup, QFontDatabase
from PySide6.QtWidgets import QApplication
from .overlay_view import OverlayContent, SessionOverlay, TOKEN_COLORS
from .overlay_tracking import overlay_geometry, anchor_for_position
from .overlay_appearance import CodexAppearance, default_appearance


def system_dark():
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                return not bool(winreg.QueryValueEx(key, 'AppsUseLightTheme')[0])
        except OSError: pass
    return True


class OverlayController(QObject):
    changed=Signal()
    navigation_requested=Signal(object)
    def __init__(self, settings, native_enabled=True, appearance_path=None):
        super().__init__()
        self.settings = settings
        self.widget = SessionOverlay()
        from .overlay_shadow import OverlayShadow
        self.shadow=OverlayShadow()
        from .overlay_chrome import OverlayChrome, OverlayDetail, OverlayLinks, CalculationNote
        self.header=OverlayChrome('header');self.toolbar=OverlayChrome('toolbar');self.icon=OverlayChrome('icon');self.actions=OverlayChrome('actions')
        self.detail=OverlayDetail(self.widget.content_model)
        self.links=OverlayLinks(self.widget.content_model)
        self.calculation_note=self.widget.content_model.calculation_note=CalculationNote(self.widget.content_model)
        self.links.view.navigationRequested.connect(self.navigate_monitor)
        self.detail.view.navigationRequested.connect(self.navigation_requested)
        self.links.wheel_forwarder=self.forward_wheel
        self.chrome=(self.header,self.toolbar,self.icon,self.actions,self.detail,self.links)
        self.collapsed=False
        self._collapse_scope=None
        self.expanded=settings.value('overlay/expanded',False,type=bool)
        self.automatic_mode='monitor'
        self.popup_open=False
        self.monitor_geometry=None
        self.height_animation=QVariantAnimation(self)
        self.height_animation.setDuration(120)
        self.height_animation.setEasingCurve(QEasingCurve.OutCubic)
        self.height_animation.valueChanged.connect(self.advance_height)
        self.height_animation.finished.connect(self.refresh)
        self.view_animation=QVariantAnimation(self)
        self.view_animation.setDuration(100)
        self.view_animation.setEasingCurve(QEasingCurve.OutCubic)
        self.view_animation.valueChanged.connect(self.advance_view)
        self._height_context=None;self._height_target=None;self._animated_height=None;self._configuring_height=False
        self.quota=None;self.quota_home=None;self.quota_issue=''
        self.opacity=max(20,min(100,settings.value('overlay/opacity',94,type=int)))
        self.widget.opacity=self.opacity
        self.anchor=None
        try:
            if settings.contains('overlay/anchorX') and settings.contains('overlay/anchorY'):
                pair=(settings.value('overlay/anchorX',type=float),settings.value('overlay/anchorY',type=float))
                if all(math.isfinite(value) for value in pair):self.anchor=tuple(min(1,max(0,v)) for v in pair)
        except (TypeError,ValueError):pass
        self._legacy_anchor=self.anchor is not None and settings.value('overlay/anchorMode','')!='edge'
        self.drag_context=None;self.current_geometry=None;self.chrome_native=None
        self.actions.collapse.connect(lambda:self.set_collapsed(True))
        self.actions.expand.connect(self.toggle_expanded)
        self.actions.opacity_toggle.connect(self.toggle_opacity)
        self.icon.restore.connect(lambda:self.set_collapsed(False))
        self.toolbar.opacity_changed.connect(self.set_opacity)
        for control in self.chrome:
            control.escape.connect(self.escape)
            control.focus_requested.connect(self.focus_control)
            if hasattr(control,'interaction_started'):
                control.interaction_started.connect(lambda control=control:self.activate_control(control))
        for control in (self.header,self.icon):
            control.drag_started.connect(self.begin_drag)
            control.drag_moved.connect(self.move_drag)
            control.drag_finished.connect(self.end_drag)
            control.move_requested.connect(self.move_keyboard)
        self.native = self.tracker = None
        self.enabled = settings.value('overlay/enabled', True, type=bool)
        self.position = settings.value('overlay/position', 'bottom-right')
        if self.position not in ('top-right', 'bottom-right'): self.position = 'bottom-right'
        self.target_state = {}
        self.observed_at = self.snapshot_at = 0
        self.snapshot_wall_time = None
        self.header.open_session.connect(self.open_session)
        self.sessions = []
        self.session_lookup = {}
        self.loading = True
        self.errors = []
        self.issue = ''
        self.dark = system_dark()
        self.reduced_motion=False
        self.appearance_reader = CodexAppearance(appearance_path, QFontDatabase.families())
        self.appearance = default_appearance(self.dark)
        self.next_theme = 0
        self.stopped = False
        self.enabled_changed = None
        self.widget.winId()
        # Qt 5 can apply its suggested size after SetWindowPos during WM_DPICHANGED.
        # Reanchor after that event finishes, preserving our logical dimensions.
        self.widget.windowHandle().screenChanged.connect(lambda screen: QTimer.singleShot(0, self.refresh))
        for control in (*self.chrome,self.shadow):
            control.winId()
            control.windowHandle().screenChanged.connect(lambda screen: QTimer.singleShot(0,self.refresh))
        if os.name == 'nt' and native_enabled:
            from .overlay_windows import WindowsOverlay, SelectionTracker
            self.native = WindowsOverlay()
            self.native.configure(int(self.widget.winId()))
            self.tracker = SelectionTracker()
            self.tracker.observed.connect(self.receive_target)
            if self.enabled:self.tracker.start()
        self.timer = QTimer(self.widget)
        self.timer.timeout.connect(self.refresh)
        self.input_timer=QTimer(self.widget)
        self.input_timer.timeout.connect(self.poll_popup)
        self._pointer_down=False
        if self.native and self.enabled: self.timer.start(1000)

    def navigate_monitor(self, target):
        if target.view != 'speed_alert':
            self.navigation_requested.emit(target);return
        if self.widget.content_model.open_speed_detail(target):
            self.expanded=True
            self.close_popup();self.refresh()
            self.detail.focus_control('detailScroll')

    def open_session(self):
        from .overlay_navigation import navigation_target
        data,_=self.content()
        target=navigation_target(data,'session')
        pressed=getattr(self.header,'pressed_scope',None)
        if target and self.selection_confirmed() and (pressed is None or pressed==(target.home,target.sid)):self.navigation_requested.emit(target)

    def selected_scope(self,home,sid):
        selection=self.target_state.get('selection')
        matches=self.session_lookup.get(sid,[])
        return bool(selection and selection.host=='local' and selection.thread_id==sid and len(matches)==1 and matches[0]['home']==home)

    def selection_confirmed(self):
        selection=self.target_state.get('selection')
        return bool(selection and selection.host=='local' and len(self.session_lookup.get(selection.thread_id,[]))==1
                    and time.monotonic()-self.observed_at<=3)

    def navigation_availability(self, home, sid):
        if not self.enabled:return dict(enabled=False,reason='오버레이 꺼짐')
        target=self.target_state.get('target');selection=self.target_state.get('selection')
        if (not self.native or not target or not selection or selection.host!='local'
                or not selection.thread_id or time.monotonic()-self.observed_at>3):
            return dict(enabled=False,reason='현재 세션 확인 불가')
        if not self.selected_scope(home,sid):
            return dict(enabled=False,reason='Codex의 현재 세션과 다름')
        return dict(enabled=True,reason='')

    def navigate_from_dashboard(self, value):
        from .overlay_navigation import NavigationTarget
        target=NavigationTarget.from_value(value)
        available=self.navigation_availability(target.home,target.sid)
        if not available['enabled']:return available
        window=self.target_state['target']
        # Re-read current route and process identity immediately before activation.
        if not hasattr(self.native,'confirm_selection'):
            return dict(enabled=False,reason='현재 세션 확인 불가')
        selection=self.native.confirm_selection(window)
        if not selection or selection.host!='local' or selection.thread_id!=target.sid:
            return dict(enabled=False,reason='Codex의 현재 세션과 다름' if selection else '현재 세션 확인 불가')
        data=next((s for s in self.session_lookup.get(target.sid,[]) if s['home']==target.home),None)
        rows=(data or {}).get('all_calls',(data or {}).get('recent',[]))
        call_id=target.call_id
        if target.event_id:
            event=next((e for e in (data or {}).get('cache_degradation',{}).get('events',[]) if str(e.get('id'))==target.event_id),None)
            if not event:return dict(enabled=False,reason='대상 기록을 찾을 수 없음')
            keys=event.get('occurrence_keys',event.get('keys',[]));call_id=keys[0] if keys else None
        if call_id and not any(str(self.widget.content_model.call_id(row))==str(call_id) for row in rows):
            return dict(enabled=False,reason='대상 기록을 찾을 수 없음')
        if not self.native.activate_target(window['hwnd']):
            return dict(enabled=False,reason='현재 세션 확인 불가')
        self.collapsed=False;self.expanded=True
        self._store_collapsed()
        self.settings.setValue('overlay/expanded',True)
        self.observed_at=time.monotonic();self.target_state={**self.target_state,'selection':selection}
        self.refresh()
        if call_id:
            self.widget.content_model.select(call_id)
        return dict(enabled=True,reason='')

    def forward_wheel(self,event):
        target=self.target_state.get('target')
        if self.native and target:self.native.forward_wheel(target['hwnd'],event.angleDelta(),event.globalPosition(),event.modifiers())

    def can_present(self,home,sid):
        return bool(self.enabled and self.selected_scope(home,sid) and self.widget.isVisible()
                    and not self.widget.note
                    and self.native and self.native.visible_target(self.target_state['target']['hwnd'])
                    and time.monotonic()-self.observed_at<=3)

    def receive_quota(self,value,home):
        self.quota=value.get('quota');self.quota_home=home;self.quota_issue=value.get('issue','')
        self.refresh()

    def quota_text(self,data):
        from .quota import quota_display
        from .model_evidence import home_key
        if not data or not self.quota_home or home_key(data['home'])!=home_key(self.quota_home):
            return ('','')
        modes=[(mode,label) for mode,label in [('five_hour','5시간'),('weekly','주간')]
               if self.quota and (mode in self.quota.get('windows',{}) or mode in self.quota.get('unlimited_windows',[]))]
        displays=[quota_display(self.quota,m,time.time()) for m,_ in modes]
        if not displays:return ('',self.quota_issue)
        text='계정 · '+' · '.join(label+' '+('—' if d['text']=='?' else d['text'])+
            ('%' if d['remaining'] is not None else '') for (_,label),d in zip(modes,displays))
        tips=' '.join(d['tooltip'] for d in displays)
        state=('초기화 후 확인 중' if any(d.get('state')=='초기화 후 확인 중' for d in displays) or '리셋권' in self.quota_issue
               else '갱신 지연' if self.quota_issue or '갱신 지연' in tips else '')
        return text,state

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.settings.setValue('overlay/enabled', self.enabled)
        if self.enabled_changed: self.enabled_changed(self.enabled)
        self.changed.emit()
        if self.tracker:
            if self.enabled and not self.tracker.isRunning():
                self.target_state={};self.observed_at=0;self.tracker.start()
            elif not self.enabled and self.tracker.isRunning():self.tracker.stop()
        if self.native and self.enabled:self.timer.start(1000)
        else:self.timer.stop()
        self.refresh()

    def toggle_expanded(self):
        self.close_popup()
        self.expanded=not self.expanded
        if not self.expanded:
            self.widget.content_model.speed_detail=False
            self.widget.content_model.sync_details()
        self.settings.setValue('overlay/expanded',self.expanded)
        self.changed.emit();self.refresh()

    def advance_height(self,value):
        self._animated_height=round(float(value))
        if not self._configuring_height:self.refresh()

    def _session_collapse_key(self):
        selection=self.target_state.get('selection')
        if not selection or selection.host!='local':return None
        matches=self.session_lookup.get(selection.thread_id,[])
        if len(matches)!=1:return None
        import hashlib
        home=os.path.normcase(os.path.abspath(matches[0]['home']))
        identity=home+'\0'+selection.thread_id
        return 'overlay/sessions/'+hashlib.sha256(identity.encode('utf-8')).hexdigest()+'/collapsed'

    def _store_collapsed(self):
        key=self._session_collapse_key()
        if key:self.settings.setValue(key,self.collapsed);self._collapse_scope=key

    def _load_collapsed(self):
        key=self._session_collapse_key()
        if key!=self._collapse_scope:
            self._collapse_scope=key
            self.collapsed=self.settings.value(key,False,type=bool) if key else False
            self.view_animation.stop();self.advance_view(1.)

    def set_collapsed(self, collapsed):
        keyboard=self.actions.keyboard_focus() if collapsed else self.icon.keyboard_focus()
        previous_mode=self.automatic_mode
        was_visible=any(control.isVisible() for control in (self.widget,self.icon))
        self.close_popup()
        self.height_animation.stop()
        target=self.target_state.get('target')
        if self.native and target and hasattr(self.native,'restore_target_focus'):
            self.native.restore_target_focus(target['hwnd'])
        self.collapsed=bool(collapsed);self._store_collapsed()
        self.view_animation.stop()
        self.changed.emit();self.refresh()
        if previous_mode!=self.automatic_mode and was_visible and not (self.native and hasattr(self.native,'reduce_motion') and self.native.reduce_motion()):
            self.view_animation.setStartValue(0.)
            self.view_animation.setEndValue(1.)
            self.view_animation.start()
        else:self.advance_view(1.)
        if keyboard:self.focus_control('restore' if self.automatic_mode=='icon' else 'expand')

    def advance_view(self, value):
        for control in (self.widget,self.shadow,*self.chrome):control.setWindowOpacity(float(value))

    def set_opacity(self, opacity):
        self.opacity=max(20,min(100,int(opacity)));self.widget.opacity=self.opacity
        self.settings.setValue('overlay/opacity',self.opacity)
        self.widget.update();self.changed.emit();self.refresh()

    def reset_position(self):
        self.anchor=None;self._legacy_anchor=False;self.position='bottom-right'
        self.settings.remove('overlay/anchorX');self.settings.remove('overlay/anchorY')
        self.settings.remove('overlay/anchorMode')
        self.settings.setValue('overlay/position',self.position)
        self.changed.emit();self.refresh()

    def begin_drag(self,position=None):
        if self.native and self.current_geometry:
            self.close_popup()
            geometry=self.current_geometry if self.automatic_mode=='icon' else self.monitor_geometry
            self.drag_context=(position or self.native.cursor(),geometry,self.target_state.get('target',{}).get('hwnd'))

    def move_keyboard(self, dx, dy):
        target=self.target_state.get('target')
        if not target or not self.native:return
        dpi=self.native.u.GetDpiForWindow(target['hwnd']) or 96
        self.begin_drag((0,0))
        self.end_drag((round(dx*dpi/96),round(dy*dpi/96)))

    def move_drag(self,position=None):
        if not self.drag_context or not self.native:return
        cursor,geometry,hwnd=self.drag_context
        if hwnd != self.target_state.get('target',{}).get('hwnd'):return
        frame=self.native.frame(hwnd)
        if not frame:return
        current=position or self.native.cursor();dpi=self.native.u.GetDpiForWindow(hwnd) or 96
        from .overlay_tracking import monitor_anchor_for_position
        self.anchor=monitor_anchor_for_position(frame,geometry,geometry[0]+current[0]-cursor[0],
                      geometry[1]+current[1]-cursor[1],dpi,
                      reference_width=380*self.appearance.scale,reference_height=560*self.appearance.scale)
        if not self.move_only(frame,dpi):self.refresh()

    def move_only(self,frame,dpi):
        if (getattr(self,'_placement_context',None)!=(frame,dpi) or not self.enabled
                or not self.selection_confirmed() or not self.native.visible_target(self.drag_context[2])
                or self.height_animation.state()==QVariantAnimation.Running):return False
        from .overlay_tracking import anchored_monitor_geometry,extend_monitor_left
        old=self.current_geometry if self.automatic_mode=='icon' else self.monitor_geometry
        box=anchored_monitor_geometry(frame,dpi,old[2]*96/dpi,old[3]*96/dpi,anchor=self.anchor)
        if box is None:return False
        if self.expanded and self.automatic_mode!='icon':
            inline=extend_monitor_left(frame,dpi,box,240*self.appearance.scale) is None
            if inline!=(self.automatic_mode=='detail-inline'):return False
        dx,dy=box[0]-old[0],box[1]-old[1]
        if not dx and not dy:return True
        shifted={hwnd:(x+dx,y+dy,w,h) for hwnd,(x,y,w,h) in self._placements.items()}
        if hasattr(self.native,'place_many'):ok=self.native.place_many(shifted.items())
        else:ok=all(self.native.place(hwnd,bounds) for hwnd,bounds in shifted.items())
        if not ok:self.hide_all();return True
        self._placements=shifted
        for name in ('current_geometry','monitor_geometry','actions_geometry','popup_geometry'):
            bounds=getattr(self,name,None)
            if bounds:setattr(self,name,(bounds[0]+dx,bounds[1]+dy,*bounds[2:]))
        if self.shadow.isVisible():
            self.shadow.clip_to_frame(frame,self.shadow.physical_geometry(self.current_geometry,dpi/96),dpi/96)
        return True

    def _place(self,hwnd,geometry):
        self._placements[hwnd]=geometry
        return self.native.place(hwnd,geometry)

    def end_drag(self,position=None):
        self.move_drag(position)
        self.drag_context=None
        if self.anchor is not None:
            self.settings.setValue('overlay/anchorX',self.anchor[0]);self.settings.setValue('overlay/anchorY',self.anchor[1])
            self.settings.setValue('overlay/anchorMode','edge');self._legacy_anchor=False
            self.changed.emit()

    def hide_all(self):
        self.height_animation.stop();self._height_context=None
        self.view_animation.stop();self.advance_view(1.)
        self.close_popup()
        self.widget.hide();self.shadow.hide()
        for control in self.chrome:control.hide()

    def receive_target(self, state):
        if state.get('selection')!=self.target_state.get('selection'):
            self.close_popup()
            self.widget.set_content(None,'기록 확인 중',appearance=self.appearance)
        self.target_state = state
        self.observed_at = time.monotonic()
        self.refresh()

    def set_position(self, position):
        if position not in ('top-right', 'bottom-right'): return
        self.position = position
        self.anchor=None;self._legacy_anchor=False
        self.settings.setValue('overlay/position', position)
        self.refresh()

    def receive_snapshot(self, snapshot):
        self.sessions = snapshot.get('overlay_sessions', [])
        self.session_lookup={}
        for s in self.sessions:self.session_lookup.setdefault(s['id'],[]).append(s)
        self.snapshot_at = time.monotonic()
        self.loading = snapshot.get('index', {}).get('loading', False)
        self.errors = snapshot.get('errors', [])
        if not self.errors:self.snapshot_wall_time = time.time()
        self.refresh()

    def content(self):
        selection = self.target_state.get('selection')
        if not selection or not selection.thread_id:
            return None, '기록 확인 중'
        if selection.host != 'local':
            return None, '원격 작업 · 로컬 기록 없음'
        matches = self.session_lookup.get(selection.thread_id,[])
        if len(matches) != 1:
            return None, '기록 확인 중' if self.loading else '호출 기록 없음' if not matches else '현재 세션 식별 불가'
        note = ('수집 오류' if self.errors else '수집 지연' if time.monotonic()-self.snapshot_at > 10
                else '기록 확인 중' if self.loading else '')
        data={**matches[0], '_collection': {
            'last_confirmed_at': self.snapshot_wall_time,
            'errors': list(self.errors), 'loading': self.loading,
            'delayed': time.monotonic()-self.snapshot_at > 10,
        }}
        return data, note

    def activate_control(self, control):
        if self.native and hasattr(self.native,'activate_companion'):
            self.native.activate_companion(int(control.winId()))

    def focus_control(self, name):
        if name in ('monitorLinks','monitorLinksLast'):
            if self.links.isVisible():
                self.activate_control(self.links);self.links.focus_control(name);return
            name='dragTitle' if name=='monitorLinks' else 'collapse'
        control=(self.detail if name in ('detailGraph','detailScroll') else
                 self.header if name=='dragTitle' else self.icon if name=='restore' else self.actions)
        if not control.isVisible():return
        self.activate_control(control)
        control.focus_control(name)

    def close_popup(self):
        self.popup_open=False
        self.toolbar.hide()
        if hasattr(self,'input_timer'):self.input_timer.stop()
        if hasattr(self.actions,'view'):self.actions.view.put(popupOpen=False)

    def toggle_opacity(self):
        if self.popup_open:
            self.close_popup()
        else:
            self.popup_open=True
            self._pointer_down=self.native.primary_down() if self.native and hasattr(self.native,'primary_down') else False
            self.input_timer.start(40)
        self.refresh()
        if self.popup_open:
            self.activate_control(self.toolbar)
            self.toolbar.focus_control()

    def escape(self):
        if self.popup_open:
            self.close_popup()
            self.focus_control('opacityButton')
            return
        elif self.expanded:
            self.toggle_expanded()
            self.focus_control('expand')
            return
        target=self.target_state.get('target')
        if self.native and target and hasattr(self.native,'restore_target_focus'):
            self.native.restore_target_focus(target['hwnd'])

    def poll_popup(self):
        if not self.popup_open or not self.native:return
        target=self.target_state.get('target')
        if not target or not self.native.visible_target(target['hwnd']):
            self.hide_all();return
        if not hasattr(self.native,'primary_down'):return
        pressed=self.native.primary_down()
        if pressed and not self._pointer_down:
            x,y=self.native.cursor()
            areas=(getattr(self,'popup_geometry',None),getattr(self,'actions_geometry',None))
            if not any(area and area[0]<=x<area[0]+area[2] and area[1]<=y<area[1]+area[3] for area in areas):
                self.close_popup()
        self._pointer_down=pressed

    def refresh(self):
        if self.stopped: return
        target = self.target_state.get('target')
        selection = self.target_state.get('selection')
        self.issue = self.target_state.get('issue', '')
        if (not self.enabled or not self.native or not target or not selection
                or not selection.thread_id or time.monotonic()-self.observed_at > 3
                or not self.native.visible_target(target['hwnd'])):
            self.hide_all(); return
        self._placements={}
        frame = self.native.frame(target['hwnd'])
        if not frame:
            self.hide_all(); return
        dpi = self.native.u.GetDpiForWindow(target['hwnd']) or 96
        self._placement_context=(frame,dpi)
        self._load_collapsed()
        data, note = self.content()
        if time.monotonic() >= self.next_theme:
            mode = self.settings.value('ui/theme', self.settings.value('overlay/theme', 'codex'))
            if mode == 'codex':
                self.appearance = self.appearance_reader.read(system_dark())
            else:
                self.appearance = default_appearance(system_dark() if mode == 'system' else mode == 'dark')
            self.dark = self.appearance.dark
            self.reduced_motion=bool(hasattr(self.native,'reduce_motion') and self.native.reduce_motion())
            self.next_theme = time.monotonic()+1
        self.widget.set_content(data, note, appearance=self.appearance)
        from .overlay_tracking import anchored_monitor_geometry, extend_monitor_left
        content=self.widget.content_model
        scale=self.appearance.scale
        monitor_width=380*scale
        if self._legacy_anchor:
            from .overlay_tracking import edge_anchor_from_legacy
            # Interpret saved free-space anchors with the previous monitor height.
            self.anchor=edge_anchor_from_legacy(frame,dpi,monitor_width,580*scale,self.anchor)
            self.settings.setValue('overlay/anchorX',self.anchor[0]);self.settings.setValue('overlay/anchorY',self.anchor[1])
            self.settings.setValue('overlay/anchorMode','edge');self._legacy_anchor=False
        full_height=content.monitor_height(reduced=False)
        def monitor_box(width,height):
            return anchored_monitor_geometry(frame,dpi,width,height,reference_width=monitor_width,
                       reference_height=560*scale,position=self.position,anchor=self.anchor)
        monitor=monitor_box(monitor_width,full_height)
        reduced=False
        if monitor is None:
            reduced=True
            monitor=monitor_box(monitor_width,content.monitor_height(reduced=True))
        if monitor is not None and not self.collapsed:
            context=((data or {}).get('home'),(data or {}).get('id'),reduced,dpi,scale,frame[2]-frame[0],frame[3]-frame[1])
            if context!=self._height_context:
                self.height_animation.stop();self._height_context=context;self._height_target=monitor[3]
            elif monitor[3]!=self._height_target:
                self.height_animation.stop();self._height_target=monitor[3]
                if (self.widget.isVisible() and self.monitor_geometry and not self.reduced_motion
                        and abs(self.monitor_geometry[3]-monitor[3])<=round(40*scale*dpi/96)):
                    self.height_animation.setStartValue(self.monitor_geometry[3]);self.height_animation.setEndValue(monitor[3])
                    self._configuring_height=True
                    try:self.height_animation.start()
                    finally:self._configuring_height=False
            if self.height_animation.state()==QVariantAnimation.Running and self._animated_height is not None:
                monitor=(monitor[0],monitor[1]+monitor[3]-self._animated_height,monitor[2],self._animated_height)
        mode='compact' if reduced else 'monitor'
        inline=False
        geometry=monitor
        if self.collapsed or monitor is None:
            mode='icon'
            geometry=monitor_box(32*scale,32*scale)
        elif self.expanded:
            side=extend_monitor_left(frame,dpi,monitor,240*scale)
            inline=side is None
            geometry=side or monitor
            mode='detail-inline' if inline else 'detail'
        content.set_layout(reduced=reduced,detail=mode in ('detail','detail-inline'),inline=inline)
        previous_mode=self.automatic_mode
        self.automatic_mode=mode
        self.widget.resize(self.widget.panel_width(),self.widget.panel_height())
        if geometry is None:
            self.hide_all(); return
        self.current_geometry=geometry
        self.monitor_geometry=monitor
        if self.chrome_native is not self.native:
            self.native.configure(int(self.widget.winId()),click_through=True)
            self.native.configure(int(self.shadow.winId()),click_through=True)
            for control in self.chrome:self.native.configure(int(control.winId()),click_through=False)
            if hasattr(self.native,'set_companions'):
                self.native.set_companions(int(control.winId()) for control in (*self.chrome,self.calculation_note))
            self.chrome_native=self.native
        for control in self.chrome:
            if hasattr(control,'apply_appearance'):control.apply_appearance(self.appearance,self.opacity)
            if hasattr(control,'view'):control.view.put(reducedMotion=self.reduced_motion)
        content.put(reducedMotion=self.reduced_motion)
        self.icon.view.put(speedWarning=bool(content.speed_alert().get('active')),
                           warning=self.widget.content_model.state.get('overlayWarning'))
        self.header.set_title(data.get('title','') if data else '',content.context()[0])
        self.header.session_scope=((data or {}).get('home'),(data or {}).get('id'))
        if mode=='icon':
            self.close_popup()
            self.widget.hide();self.shadow.hide();self.header.hide();self.actions.hide();self.detail.hide();self.links.hide()
            # Keep the target and its anchor at 32px; the transparent gutter
            # only permits the external keyboard focus ring to be painted.
            self.icon.resize(round(40*scale),round(40*scale))
            if not self.icon.isVisible():self.icon.show()
            gutter=round(4*scale*dpi/96)
            icon_geometry=(geometry[0]-gutter,geometry[1]-gutter,geometry[2]+2*gutter,geometry[3]+2*gutter)
            if not self._place(int(self.icon.winId()),icon_geometry):self.icon.hide()
        else:
            self.icon.hide()
            self.actions.view.put(expanded=self.expanded,canExpand=True,popupOpen=self.popup_open)
            self.shadow.apply_appearance(self.appearance,self.opacity)
            self.shadow.set_panel_size(self.widget.panel_width()/scale,geometry[3]/(dpi/96*scale))
            shadow_geometry=self.shadow.physical_geometry(geometry,dpi/96)
            self.shadow.clip_to_frame(frame,shadow_geometry,dpi/96)
            if not self.shadow.isVisible():self.shadow.show()
            if not self._place(int(self.shadow.winId()),shadow_geometry):self.shadow.hide()
            if not self.widget.isVisible(): self.widget.show()
            if not self._place(int(self.widget.winId()), geometry):self.hide_all();return
            native_scale=dpi/96*scale
            def place(control,area,origin=geometry):
                x,y,w,h=area
                control.resize(round(w*scale),round(h*scale))
                if not control.isVisible():control.show()
                placed=(origin[0]+round(x*native_scale),origin[1]+round(y*native_scale),round(w*native_scale),round(h*native_scale))
                if not self._place(int(control.winId()),placed):control.hide()
                return placed
            if mode in ('detail','detail-inline'):
                place(self.detail,(0,0,380 if inline else 240,geometry[3]/native_scale))
            else:self.detail.hide()
            if not inline and self.links.sync():place(self.links,(0,0,380,geometry[3]/native_scale),monitor)
            else:self.links.hide()
            place(self.header,(12,8,268,36),monitor)
            self.actions_geometry=place(self.actions,(280,8,88,32),monitor)
            if self.popup_open:
                # Align below the header. The monitor already guarantees enough
                # interior width; clamping also handles rounded native pixels.
                popup_w,popup_h=round(160*native_scale),round(40*native_scale)
                px=monitor[0]+round(204*native_scale)
                py=monitor[1]+round(44*native_scale)
                gap=round(16*dpi/96)
                px=min(frame[2]-gap-popup_w,max(frame[0]+gap,px))
                py=min(frame[3]-gap-popup_h,max(frame[1]+gap,py))
                self.toolbar.resize(round(160*scale),round(40*scale))
                self.popup_geometry=(px,py,popup_w,popup_h)
                if not self.toolbar.isVisible():self.toolbar.show()
                if not self._place(int(self.toolbar.winId()),self.popup_geometry):self.close_popup()
            else:self.toolbar.hide()
            if mode!=previous_mode and hasattr(self.native,'raise_companion'):
                for control in (self.header,self.actions,self.toolbar):
                    if control.isVisible():self.native.raise_companion(int(control.winId()))

    def stop(self):
        if self.stopped: return
        self.stopped = True
        self.height_animation.stop()
        self.view_animation.stop()
        self.timer.stop(); self.input_timer.stop(); self.hide_all()
        if self.tracker: self.tracker.stop()
        self.widget.close()
        self.shadow.close()
        for control in self.chrome:control.close()


def install_overlay(window, native_enabled=True):
    from PySide6.QtWidgets import QMenu
    controller = OverlayController(window.settings, native_enabled=native_enabled)
    window.overlay = controller
    if hasattr(window,'navigate'):controller.navigation_requested.connect(window.navigate)
    if hasattr(window,'refresh_overlay_availability'):controller.changed.connect(window.refresh_overlay_availability)
    if getattr(window,'quota_service',None):
        window.quota_service.updated.connect(lambda value:controller.receive_quota(value,window.quota_service.home))
        controller.receive_quota({'quota':window.live_quota,'issue':getattr(window,'quota_issue','')},window.quota_service.home)
    menu = window.tray.contextMenu()
    before = getattr(window, 'exit_action', window.startup)
    from .i18n import tr
    action = QAction(tr('세션 오버레이'), window, checkable=True)
    action.setChecked(controller.enabled)
    action.triggered.connect(controller.set_enabled)
    controller.enabled_changed = action.setChecked
    window.overlay_action = action
    theme_menu = QMenu('오버레이 테마', menu)
    group = QActionGroup(theme_menu); group.setExclusive(True)
    current = window.settings.value('ui/theme',window.settings.value('overlay/theme', 'codex'))
    def choose_theme(mode):
        from .theme import shared_theme
        window.settings.setValue('ui/theme', mode)
        shared_theme().configure(mode)
        controller.next_theme = 0
        controller.refresh()
    theme_actions = {}
    for mode, title in (('codex', 'Codex와 동일'), ('light', '밝게'), ('dark', '어둡게')):
        theme = QAction(title, group, checkable=True)
        theme.setChecked(mode == current)
        theme.triggered.connect(lambda checked, mode=mode: choose_theme(mode))
        theme_menu.addAction(theme)
        theme_actions[mode] = theme
    if hasattr(window, 'settings_page'):
        window.settings_page.bind_action('overlay', action)
        window.settings_page.bind_overlay(controller)
        window.settings_page.bind_choices('theme', theme_actions)
    else:
        menu.insertAction(before,action)
        menu.insertMenu(before, theme_menu)
        menu.insertSeparator(before)
    window.worker.failure.connect(lambda error: setattr(controller, 'errors', [error]))
    QApplication.instance().aboutToQuit.connect(controller.stop)
    return controller
