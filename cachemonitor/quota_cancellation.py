"""Cooperative cancellation of disposable reads, never durable edits."""
from contextvars import ContextVar

poll = ContextVar('quota_read_poll', default=None)


class ReadCancelled(BaseException):
    pass


def checkpoint():
    callback = poll.get()
    if callback is not None and callback():
        raise ReadCancelled()
