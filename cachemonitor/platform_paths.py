"""Platform storage and path identity, independent of Qt and service lifetimes."""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import posixpath
import sys


def app_data_dir():
    override = os.environ.get('CODEXON_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Codexon'
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'CacheMonitor'


def observer_data_dir():
    if sys.platform == 'darwin' or os.environ.get('CODEXON_DATA_DIR'):
        return app_data_dir() / 'model-observer'
    # Windows MSIX and ordinary launches must share this existing location.
    return Path.home() / '.cachemonitor' / 'model-observer'


def launch_preferences_path():
    if sys.platform == 'darwin' or os.environ.get('CODEXON_DATA_DIR'):
        return app_data_dir() / 'launch.json'
    return Path.home() / '.cachemonitor' / 'launch.json'


def windows_path(path):
    """Recognize recorded Windows paths even when viewing them on a Mac."""
    return bool(ntpath.splitdrive(path)[0] or path.startswith('\\\\'))


def path_identity(path):
    if not path:
        return ''
    path = str(path)
    if windows_path(path):
        return ntpath.normcase(ntpath.normpath(path.removeprefix('\\\\?\\')))
    # Never merge distinct directories on a case-sensitive APFS volume.
    return posixpath.normpath(path)


def path_within(path, root):
    path, root = path_identity(path), path_identity(root)
    if not path or not root:
        return False
    separator = '\\' if windows_path(root) else '/'
    return path == root or path.startswith(root.rstrip(separator) + separator)


def parent_path(path):
    return ntpath.dirname(path) if windows_path(path) else posixpath.dirname(path)


def instance_name():
    identity = str(os.getuid()) if hasattr(os, 'getuid') else os.environ.get('USERNAME', 'user')
    return 'CacheMonitor-' + identity
