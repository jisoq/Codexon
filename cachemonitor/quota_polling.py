"""Bound account reads during activity, inactivity, outages and resume."""


class QuotaPolling:
    ACTIVE = 15
    IDLE = 60
    MINIMUM = 5
    RETRIES = (15, 30, 60, 120, 300)

    def __init__(self):
        self.next_read = 0
        self.last_attempt = float('-inf')
        self.last_activity = float('-inf')
        self.signature = None
        self.failures = 0
        self.last_wall = None
        self.active = False

    def tick(self, now, wall):
        resumed = self.last_wall is not None and (wall-self.last_wall > 120 or wall < self.last_wall-1)
        self.last_wall = wall
        if resumed:
            self.next_read = now
            self.failures = 0
        return resumed

    def activity(self, signature, active, now):
        changed = signature != self.signature
        self.signature = signature
        self.active = active
        if changed:
            self.last_activity = now
        if not self.failures and (changed or active):
            delay = self.MINIMUM if changed else self.ACTIVE
            self.next_read = min(self.next_read, max(now, self.last_attempt+delay))

    def ready(self, now):
        return now >= self.next_read

    def finish(self, now, success, quota=None, wall=None):
        self.last_attempt = now
        self.failures = 0 if success else self.failures+1
        if self.failures:
            delay = self.RETRIES[min(self.failures-1, len(self.RETRIES)-1)]
        else:
            delay = self.ACTIVE if self.active or now-self.last_activity < 60 else self.IDLE
            # Query once the observed reset boundary passes, even while idle.
            if quota and wall is not None:
                resets = [w.get('resets_at') for w in quota.get('windows', {}).values()]
                future = [r-wall+1 for r in resets if r and r > wall]
                if future:
                    delay = max(self.MINIMUM, min(delay, min(future)))
        self.next_read = now+delay
