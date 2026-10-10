"""Local-only packages and stale installs must fail before expensive CI checks."""
from importlib.metadata import PackageNotFoundError

from tools.check_environment import check_environment


def repository(tmp_path,source):
    (tmp_path/'requirements-release.lock').write_text('PySide6==6.11.2\n',encoding='utf-8')
    tests=tmp_path/'tests';tests.mkdir()
    (tests/'test_example.py').write_text(source,encoding='utf-8')
    return tmp_path


def test_function_local_import_requires_a_locked_distribution(tmp_path):
    root=repository(tmp_path,'import json\nfrom PySide6 import QtCore\ndef check():\n    import yaml\n')
    result=check_environment(root,distributions={'PySide6':['PySide6'],'yaml':['PyYAML']},
                             installed_version=lambda _: '6.11.2')
    assert result['errors']==[dict(kind='unlocked_import',module='yaml',file='tests/test_example.py',line=4)]


def test_missing_and_different_versions_are_not_a_valid_ci_environment(tmp_path):
    root=repository(tmp_path,'import json\n')
    result=check_environment(root,distributions={},installed_version=lambda _: '6.10.0')
    assert result['errors']==[dict(kind='version_mismatch',package='PySide6',expected='6.11.2',actual='6.10.0')]
    def missing(_):raise PackageNotFoundError('PySide6')
    assert check_environment(root,distributions={},installed_version=missing)['errors'][0]['kind']=='missing_distribution'


def test_local_imports_and_syntax_are_checked_without_running_code(tmp_path):
    root=repository(tmp_path,"import json\nfrom test_helper import value\nraise RuntimeError('must not run')\n")
    helper=root/'tests/test_helper.py';helper.write_text('value=1\n',encoding='utf-8')
    assert not check_environment(root,distributions={},installed_version=lambda _: '6.11.2')['errors']
    helper.write_text('def invalid(\n',encoding='utf-8')
    errors=check_environment(root,distributions={},installed_version=lambda _: '6.11.2')['errors']
    assert len(errors)==1 and errors[0]['kind']=='syntax_error' and errors[0]['file']=='tests/test_helper.py'
