"""Per-user login startup without terminating an already running dashboard."""
from .observer_task import ObserverTask
from .platform_paths import app_data_dir


class MacStartup:
    def __init__(self):
        self.task = ObserverTask(str(app_data_dir()), role='Desktop')

    def enabled(self):
        value = self.task.inspect()
        return bool(value.get('registered') and value.get('autostart'))

    def set_enabled(self, enabled, command):
        self.task.configure(command, autostart=bool(enabled))
