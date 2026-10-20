from tools.ci_plan import build_plan


def test_missing_baseline_uses_essential_source_and_full_deployment_contracts():
    plan = build_plan(None, candidate=True)
    assert plan['source_args'] == ['--full']
    assert plan['source_suite'] == 'essential'
    assert plan['package'] and plan['msix'] and plan['proxy'] and plan['scales']


def test_shared_dependency_change_retains_essential_and_affected_integration_contracts():
    plan = build_plan({'requirements-release.lock', 'cachemonitor/install_activation.py'}, base='verified')
    assert plan['source_args'] == ['--full', '--base', 'verified']
    assert plan['source_suite'] == 'essential+affected'
    assert any(test.startswith('tests/test_install') for test in plan['affected_tests'])
    assert plan['dependencies'] and plan['package'] and plan['msix'] and plan['proxy']


def test_policy_only_checks_do_not_require_native_build_or_installer():
    plan = build_plan({'tools/source_receipt.py'}, base='verified')
    assert plan['tests']
    assert not plan['rust'] and not plan['package']


def test_extended_always_remains_explicit_and_includes_all_contracts():
    plan = build_plan({'docs/followup.md'}, base='verified', extended=True)
    assert plan['source_args'] == ['--extended']
    assert plan['source_suite'] == 'extended'
    assert plan['package'] and plan['rust'] and plan['proxy'] and plan['scales']


def test_source_baseline_does_not_hide_failed_deployment_contracts():
    plan = build_plan(set(), base='source-passed', package_changed={'cachemonitor/install_activation.py'})
    assert not plan['tests']
    assert plan['package'] and plan['proxy'] and plan['msix']


def test_invalid_source_baseline_keeps_known_deployment_ancestor_integration_scope():
    plan = build_plan(None, fallback_base='verified-deployment',
                      package_changed={'cachemonitor/install_activation.py'})
    assert plan['source_args'] == ['--full', '--base', 'verified-deployment']
    assert plan['source_comparison_base'] == 'verified-deployment'
    assert plan['source_suite'] == 'essential+affected'
    assert any(test.startswith('tests/test_install') for test in plan['affected_tests'])


def test_retained_ordinary_installer_passes_dependency_audit_without_forcing_a_build():
    release = build_plan({'cachemonitor/pricing.py'}, base='verified', release_artifact=True)
    assert not release['package'] and not release['release_artifact'] and not release['dependencies']
    release = build_plan({'cachemonitor/install_activation.py'}, base='verified', release_artifact=True)
    assert release['package'] and release['release_artifact'] and release['dependencies']
