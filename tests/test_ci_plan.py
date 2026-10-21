import json

import pytest

from tools import ci_plan
from tools.ci_plan import build_plan
from tools.verify_changes import CORE_TESTS


def test_missing_baseline_uses_basic_source_and_native_deployment_contracts():
    plan = build_plan(None, candidate=True)
    assert plan['source_args'] == ['--basic']
    assert plan['source_suite'] == 'basic'
    assert plan['tests'] == tuple(sorted(CORE_TESTS))
    assert plan['affected_tests'] == () and not plan['full']
    assert plan['package'] and plan['native'] and plan['rust']
    assert not any(plan[key] for key in ('msix', 'proxy', 'scales', 'gui_handoff'))


def test_shared_dependency_change_keeps_basic_source_and_packages_current_product():
    plan = build_plan({'requirements-release.lock', 'cachemonitor/install_activation.py'}, base='verified')
    assert plan['source_args'] == ['--basic']
    assert plan['source_suite'] == 'basic' and plan['affected_tests'] == ()
    assert plan['dependencies'] and plan['package'] and plan['native']
    assert not plan['msix'] and not plan['proxy']


def test_policy_only_checks_keep_fixed_basic_selection_without_an_installer():
    plan = build_plan({'tools/source_receipt.py'}, base='verified')
    assert plan['tests'] == tuple(sorted(CORE_TESTS))
    assert plan['rust'] and not plan['package']


def test_extended_always_remains_explicit_and_includes_all_contracts():
    plan = build_plan({'docs/followup.md'}, base='verified', extended=True)
    assert plan['source_args'] == ['--extended']
    assert plan['source_suite'] == 'extended' and plan['tests'] == ('tests',)
    assert all(plan[key] for key in ('package', 'rust', 'proxy', 'scales', 'gui_handoff', 'msix'))
    assert not plan['native']


def test_source_baseline_does_not_hide_failed_deployment_contracts():
    plan = build_plan(set(), base='source-passed', package_changed={'cachemonitor/install_activation.py'})
    assert plan['tests'] == tuple(sorted(CORE_TESTS))
    assert plan['package'] and plan['native']
    assert not plan['proxy'] and not plan['msix']


def test_basic_selection_does_not_depend_on_source_or_deployment_baselines():
    plan = build_plan(None, fallback_base='verified-deployment',
                      package_changed={'cachemonitor/install_activation.py'})
    assert plan['source_args'] == ['--basic']
    assert plan['source_comparison_base'] == ''
    assert plan['affected_tests'] == ()
    assert plan['tests'] == build_plan(set(), base='source-passed')['tests']


def test_retained_ordinary_installer_passes_dependency_audit_without_forcing_a_build():
    release = build_plan({'cachemonitor/pricing.py'}, base='verified', release_artifact=True)
    assert not release['package'] and not release['release_artifact'] and not release['dependencies']
    release = build_plan({'cachemonitor/install_activation.py'}, base='verified', release_artifact=True)
    assert release['package'] and release['release_artifact'] and release['dependencies']


@pytest.mark.parametrize('path', ['tools/verify_proxy_update.py', 'tools/verify_app_services.py',
                                  'tools/verify_retired_worker.py', 'tools/verify_gui_handoff.py'])
@pytest.mark.parametrize('reusable', [False, True])
def test_deferred_qa_tools_cannot_reenter_basic_through_reuse_or_fallback(path, reusable):
    plan = build_plan({path}, base='verified', reuse_qa=reusable)
    assert plan['qa_smokes'] == []
    assert not any(plan[key] for key in ('qa_reuse', 'package', 'proxy', 'gui_handoff'))
    extended = build_plan({path}, base='verified', extended=True, reuse_qa=reusable)
    assert extended['qa_smokes'] and extended['package'] and extended['proxy'] and extended['gui_handoff']


@pytest.mark.parametrize('reusable', [False, True])
def test_recovery_smoke_still_reuses_or_builds_a_matching_product(reusable):
    plan = build_plan({'tools/verify_recovery.py'}, base='verified', reuse_qa=reusable)
    assert plan['qa_smokes'] == ['recovery']
    assert plan['qa_reuse'] == reusable and plan['package'] == (not reusable)
    assert plan['native'] == (not reusable)
    assert not plan['proxy'] and not plan['gui_handoff']


def test_basic_ci_preserves_unmapped_change_failure():
    plan = build_plan({'cachemonitor/unmapped_new.py'}, package_changed={'tools/unmapped_new.py'})
    assert plan['unmapped'] == ['cachemonitor/unmapped_new.py', 'tools/unmapped_new.py']


def test_deferred_qa_does_not_probe_published_binaries(tmp_path, monkeypatch, capsys):
    paths = {'tools/verify_proxy_update.py', 'tools/verify_gui_handoff.py'}
    monkeypatch.setattr(ci_plan, 'ROOT', tmp_path)
    monkeypatch.setattr(ci_plan, 'file_hashes', lambda: dict.fromkeys(paths, 'current'))
    monkeypatch.setattr(ci_plan, 'changed_files', lambda *args, **kwargs: paths)
    monkeypatch.setattr(ci_plan, 'release_version_only', lambda **kwargs: False)
    monkeypatch.setattr(ci_plan.sys, 'argv', ['ci_plan.py', '--base', 'verified'])
    monkeypatch.delenv('GITHUB_OUTPUT', raising=False)
    def unexpected():
        raise AssertionError('Deferred smoke tools must not probe public binaries')
    monkeypatch.setattr(ci_plan, 'can_reuse', unexpected)
    ci_plan.main()
    plan = json.loads(capsys.readouterr().out)
    assert plan['source_args'] == ['--basic'] and plan['qa_smokes'] == []
    assert not plan['qa_reuse'] and not plan['package']
