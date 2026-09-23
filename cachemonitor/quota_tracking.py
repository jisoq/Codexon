"""Forward-only account observations; local task state never gates monitoring."""
from dataclasses import dataclass, field
from typing import Optional


OBSERVATION_FRESHNESS = 120
RESET_NAMES = {'scheduled_reset': '정기 초기화', 'arbitrary_reset': '임의 초기화'}


@dataclass
class ResetTracker:
    """Classify observed allowance increases using the prior reset schedule."""
    remaining: Optional[float] = None
    at: Optional[float] = None
    scheduled_at: Optional[float] = None
    consumed_at: Optional[float] = None
    recovering: bool = False

    def observe(self, remaining, at, scheduled_at, continuous=True):
        import math
        if not continuous:
            self.remaining = self.scheduled_at = self.consumed_at = None
            self.recovering = False
        schedule = scheduled_at if (type(scheduled_at) in (int, float)
            and math.isfinite(scheduled_at) and scheduled_at > 0) else None
        event = None
        # The first sample after a reset may already include new consumption.
        if self.remaining is not None and remaining < self.remaining:
            self.recovering = False
        if self.remaining is not None and remaining > self.remaining and not self.recovering:
            event = ('scheduled_reset' if self.scheduled_at is not None and at >= self.scheduled_at
                     else 'arbitrary_reset')
            self.recovering = True
            if event == 'scheduled_reset':
                self.consumed_at = self.scheduled_at
            self.scheduled_at = None
        # Keep an already-due schedule until recovery is observed, even if the
        # server advances the next deadline before the next sample arrives.
        if schedule is not None and schedule != self.consumed_at and (self.scheduled_at is None
                or schedule < self.scheduled_at or remaining == 100):
            self.scheduled_at = schedule
        self.remaining, self.at = remaining, at
        return event


@dataclass(frozen=True)
class Observation:
    sequence: int
    requested_at: float
    received_at: float
    remaining: float
    epoch: tuple
    resets_at: Optional[float] = None
    continuous: bool = True


@dataclass
class TrackingState:
    activated_at: float
    phase: str = 'on'
    active: Optional[bool] = None
    activity_at: float = 0
    finished_at: Optional[float] = None
    baseline: Optional[Observation] = None
    endpoint: Optional[Observation] = None
    idle_endpoint: Optional[Observation] = None
    last: Optional[Observation] = None
    boundary_uncertain: bool = False
    generation: int = 0
    closed: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    resets: ResetTracker = field(default_factory=ResetTracker)

    def activity(self, active, at):
        # Task lifecycle is supplementary evidence, not a switch for account
        # consumption. Historical orphan tasks cannot block the first sample.
        if at < self.activity_at:
            return False
        changed = active != self.active
        self.active, self.activity_at = active, at
        if changed:
            self.generation += 1
        return changed

    def close(self, reason, keep_reset=False):
        if self.baseline and self.endpoint:
            self.closed.append(dict(start=self.baseline, end=self.endpoint,
                                    finished_at=None, reason=reason,
                                    boundary_uncertain=False, observations=list(self.observations)))
        self.baseline = self.endpoint = self.idle_endpoint = None
        self.last = None
        self.observations.clear()
        self.phase = 'on'
        if not keep_reset:
            self.resets = ResetTracker()

    def observe(self, value):
        import math
        if (not math.isfinite(value.remaining) or not 0 <= value.remaining <= 100
                or value.received_at < value.requested_at
                or value.requested_at < self.activated_at):
            return False
        if self.last and (value.sequence <= self.last.sequence
                          or value.received_at < self.last.received_at):
            return False
        previous = self.last
        # Account and allowance changes cannot establish a reset event.
        same_account = not previous or previous.epoch[0] == value.epoch[0]
        same_limit = not previous or previous.epoch == value.epoch or (
            len(value.epoch) >= 5 and len(previous.epoch) >= 5 and
            previous.epoch[:3] + previous.epoch[4:] == value.epoch[:3] + value.epoch[4:])
        event = self.resets.observe(value.remaining, value.received_at, value.resets_at,
                                    value.continuous and same_limit)
        if event:
            self.close(event, keep_reset=True)
        elif (self.resets.recovering and self.endpoint and same_limit
              and value.remaining > self.endpoint.remaining):
            # Later samples can finish the same recovery. Use the highest
            # observed allowance as this new segment's baseline.
            self.baseline = self.endpoint = self.last = value
            self.observations = [value]
            return True
        elif self.last and self.last.epoch != value.epoch:
            # A changed timestamp alone is not a reset. Preserve consumption if
            # it is still monotone; a decrease starts a new measured segment.
            if not same_limit or value.remaining > self.endpoint.remaining:
                self.close('allowance_changed', keep_reset=same_account)
        elif self.endpoint and value.remaining > self.endpoint.remaining:
            self.close('observation_gap', keep_reset=True)
        self.last = value
        if self.baseline is None:
            self.baseline = value
        self.endpoint = value
        self.observations.append(value)
        return True
