"""Canonical usage-index locations shared by the collector and its clients."""
import os
from pathlib import Path


def index_location(path=None):
    return (Path(path) if path is not None else
            Path(os.environ.get('LOCALAPPDATA', Path.home()))/'CacheMonitor'/'usage-index.sqlite').resolve()


def legacy_index_location():
    return (Path(os.environ.get('LOCALAPPDATA', Path.home()))/'CacheSessionRegistry'/'usage-index.sqlite').resolve()
