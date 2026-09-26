"""Native notifications shared with the independent recovery process; no Qt."""
import uuid

_delegate = None


def notification_center():
    try:
        from Foundation import NSBundle
        # UserNotifications requires an app identity. Source/CLI runs retain
        # their in-app event history instead of crashing Cocoa or asking access.
        if NSBundle.mainBundle().bundleIdentifier() not in ('io.github.jisoq.codexon', 'io.github.jisoq.codexon.recovery'):
            return None, None
        import UserNotifications as un
        center = un.UNUserNotificationCenter.currentNotificationCenter()
        _install_delegate(center)
        return un, center
    except (ImportError, RuntimeError):
        return None, None


def notification_status(callback):
    """Read authorization asynchronously, without ever requesting it."""
    un, center = notification_center()
    if center is None:
        callback('unavailable')
        return
    def received(settings):
        try:
            status = int(settings.authorizationStatus()) if settings is not None else -1
            callback({0: 'not_determined', 1: 'denied', 2: 'authorized', 3: 'provisional'}.get(status, 'unavailable'))
        except Exception:
            # Exceptions must never unwind through an Objective-C completion.
            return
    center.getNotificationSettingsWithCompletionHandler_(received)


def request_notifications(callback):
    """Only call from a person's explicit Allow notifications button."""
    un, center = notification_center()
    if center is None:
        callback(False)
        return
    options = un.UNAuthorizationOptionAlert | un.UNAuthorizationOptionSound | un.UNAuthorizationOptionBadge
    def completed(granted, error):
        try:callback(bool(granted) and error is None)
        except Exception:return
    center.requestAuthorizationWithOptions_completionHandler_(options, completed)


def show_notification(title, message):
    """Send only when already authorized; the in-app event log always remains."""
    un, center = notification_center()
    if center is None:
        return False
    def received(settings):
        try:
            if settings is None or int(settings.authorizationStatus()) not in (2, 3):return
            content = un.UNMutableNotificationContent.alloc().init()
            content.setTitle_(str(title));content.setBody_(str(message))
            request = un.UNNotificationRequest.requestWithIdentifier_content_trigger_(str(uuid.uuid4()), content, None)
            center.addNotificationRequest_withCompletionHandler_(request, None)
        except Exception:return
    center.getNotificationSettingsWithCompletionHandler_(received)
    return True


def _install_delegate(center):
    global _delegate
    if _delegate is not None:return
    import objc
    from Foundation import NSObject
    class CodexonNotificationDelegate(NSObject, protocols=[objc.protocolNamed('UNUserNotificationCenterDelegate')]):
        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(self, center, response, done):
            try:
                uri = str(response.notification().request().content().userInfo().get('recovery_url', ''))
                open_recovery_uri(uri)
            except Exception:pass
            finally:done()
    _delegate = CodexonNotificationDelegate.alloc().init()
    center.setDelegate_(_delegate)


def open_recovery_uri(uri):
    """Use this installation's helper even before LaunchServices discovers it."""
    if not uri.startswith('codexon-recovery:') or len(uri)>16384:return False
    import os
    import subprocess
    from .installation import recovery_command
    subprocess.Popen([*recovery_command(),uri],start_new_session=True,
                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     env={**os.environ,'PYINSTALLER_RESET_ENVIRONMENT':'1'})
    return True


def notify_recovery(manager, result):
    """Use the main app's existing notification grant; never request one here."""
    import subprocess
    from .installation import installed
    from .connection_recovery import notification_uri
    receipt = installed()
    if not receipt:return False
    try:
        outcome = subprocess.run([receipt['AppPath'], '--recovery-notification', notification_uri(manager),
                                  '--title', result['title'], '--message', result['detail']],
                                  capture_output=True, timeout=10)
        return outcome.returncode == 0
    except (OSError, subprocess.TimeoutExpired):return False


def recovery_notification_main(argv=None):
    import argparse
    import time
    from Foundation import NSDate, NSRunLoop
    parser=argparse.ArgumentParser()
    parser.add_argument('uri');parser.add_argument('--title',required=True);parser.add_argument('--message',required=True)
    args=parser.parse_args(argv)
    if not args.uri.startswith('codexon-recovery:') or len(args.uri)>16384:return 1
    un,center=notification_center()
    if center is None:return 1
    state={'done':False,'success':False}
    def delivered(error):
        state.update(done=True,success=error is None)
    def settings_received(settings):
        try:
            if settings is None or int(settings.authorizationStatus()) not in (2,3):
                state['done']=True;return
            content=un.UNMutableNotificationContent.alloc().init()
            content.setTitle_(args.title);content.setBody_(args.message)
            content.setUserInfo_({'recovery_url':args.uri})
            request=un.UNNotificationRequest.requestWithIdentifier_content_trigger_(str(uuid.uuid4()),content,None)
            center.addNotificationRequest_withCompletionHandler_(request,delivered)
        except Exception:state['done']=True
    center.getNotificationSettingsWithCompletionHandler_(settings_received)
    deadline=time.monotonic()+5
    while not state['done'] and time.monotonic()<deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.05))
    return 0 if state['success'] else 1
