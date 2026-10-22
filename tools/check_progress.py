"""Pytest phase evidence independent of capture and buffered Actions output."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from tools.check_diagnostics import DIAGNOSTIC_EXIT, DiagnosticFailure, diagnostic_io, status


_active_event = None
_product_failed = False


def pytest_addoption(parser):
    parser.addoption('--affected-test', action='append', default=[],
                     help='Include this affected integration contract in the essential sweep')


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    affected = config.getoption('--affected-test')
    for item in items:
        if any(item.nodeid == target or item.nodeid.startswith(target + '::')
               or item.nodeid.startswith(target + '[') for target in affected):
            item.add_marker('essential')


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    outcome = yield
    if fixturedef.argname == 'tmp_path_factory' and os.environ.get('CODEXON_CHECK_DIAGNOSTICS'):
        from tools.check_processes import register_root
        # Pytest allocates this session-owned directory before any test starts
        # a scheduled task. Include both tmp_path and mktemp descendants.
        try:
            register_root(outcome.get_result().getbasetemp())
        except DiagnosticFailure as error:
            pytest.exit(str(error), returncode=1 if _product_failed else DIAGNOSTIC_EXIT)


def phase(item, name):
    global _active_event
    event = dict(test=item.nodeid, phase=name, started=datetime.now(timezone.utc).isoformat(), pid=os.getpid())
    _active_event = event
    save_phase()
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


def save_phase():
    folder = os.environ.get('CODEXON_CHECK_DIAGNOSTICS')
    if not folder or status()['first_error']:
        return
    path = Path(folder) / 'active-test.json'
    temporary = path.with_suffix('.tmp')
    try:
        with diagnostic_io('write active test', temporary):
            temporary.write_text(json.dumps(_active_event), encoding='utf-8')
            temporary.replace(path)
    except DiagnosticFailure as error:
        pytest.exit(str(error), returncode=1 if _product_failed else DIAGNOSTIC_EXIT)


def pytest_runtest_logreport(report):
    global _product_failed
    _product_failed = _product_failed or report.failed


def pytest_runtest_logfinish(nodeid, location):
    if status()['first_error']:
        pytest.exit(status()['first_error']['message'], returncode=1 if _product_failed else DIAGNOSTIC_EXIT)
    if _active_event is not None:
        _active_event.update(phase='finished', finished=datetime.now(timezone.utc).isoformat())
        save_phase()
