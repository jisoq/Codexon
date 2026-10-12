"""Pytest phase evidence independent of capture and buffered Actions output."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest


def phase(item, name):
    event = dict(test=item.nodeid, phase=name, started=datetime.now(timezone.utc).isoformat(), pid=os.getpid())
    folder = os.environ.get('CODEXON_CHECK_DIAGNOSTICS')
    if folder:
        path = Path(folder) / 'active-test.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(event), encoding='utf-8')
        temporary.replace(path)
    # The terminal reporter temporarily suspends FD capture too.
    reporter = item.config.pluginmanager.getplugin('terminalreporter')
    if reporter:
        with item.config.pluginmanager.getplugin('capturemanager').global_and_fixture_disabled():
            reporter.write_line(f"[check {event['started']}] {item.nodeid} {name}")
            reporter._tw.flush()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    phase(item, 'setup')


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    phase(item, 'call')


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    phase(item, 'teardown')


def pytest_runtest_logfinish(nodeid, location):
    folder = os.environ.get('CODEXON_CHECK_DIAGNOSTICS')
    if folder:
        path = Path(folder) / 'active-test.json'
        event = json.loads(path.read_text(encoding='utf-8'))
        event.update(phase='finished', finished=datetime.now(timezone.utc).isoformat())
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(event), encoding='utf-8')
        temporary.replace(path)
