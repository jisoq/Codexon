import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from cachemonitor import connection_recovery as recovery, install_dispatch as dispatch


@pytest.mark.parametrize('native,packaged',[(False,False),(False,True),(True,False),(True,True)])
def test_installer_mutates_only_the_native_environment(tmp_path,monkeypatch,native,packaged):
    calls=[]
    monkeypatch.setattr(dispatch,'packaged_context',lambda:packaged)
    monkeypatch.setattr(dispatch,'native_install',lambda args:(calls.append('dispatch') or dict(version='synthetic')))
    monkeypatch.setattr('cachemonitor.install_management.finish',lambda *a,**k:(calls.append('finish') or dict(version='synthetic')))
    report=tmp_path/'result.json'
    args=['--install-root',str(tmp_path),'--product-dir',str(tmp_path/'product'),'--report',str(report)]
    if native:args.append('--native-install')
    assert recovery.main(args)==int(native and packaged)
    assert calls==([] if native and packaged else ['dispatch'] if not native else ['finish'])
    assert bool(json.loads(report.read_text(encoding='utf-8')).get('error'))==(native and packaged)


@pytest.mark.parametrize('result',[{'version':'synthetic'},{'error':'synthetic failure'},None])
def test_native_activation_requires_its_own_completed_receipt(tmp_path,monkeypatch,result):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    calls=[]
    class Task:
        def __init__(self,scope,role):self.report=Path(scope);assert role=='InstallationActivation'
        def start(self,command):
            calls.append(command)
            assert command[command.index('--report')+1]==str(self.report)
            assert '--native-install' in command and '--no-launch' in command and '--isolated-install' in command
            assert command[command.index('--language')+1]=='en'
            assert command[command.index('--update-id')+1]=='a'*32
            if result is not None:self.report.write_text(json.dumps(result))
        def inspect(self):return dict(running=0,state=3,last_result=1 if result and result.get('error') else 0)
        def remove(self):calls.append('removed')
    monkeypatch.setattr(dispatch,'ObserverTask',Task)
    args=SimpleNamespace(install_root=tmp_path/'installed',product_dir=tmp_path/'new',language='en',
                         no_launch=True,isolated_install=True,prepare_uninstall=False,update_id='a'*32)
    if result is None:
        with pytest.raises(RuntimeError,match='결과 없이'):dispatch.native_install(args)
    else:assert dispatch.native_install(args)==result
    assert calls[-1]=='removed'


def test_shared_installation_pointer_overrides_stale_registry_view(tmp_path,monkeypatch):
    from cachemonitor import installation
    root=tmp_path/'installed'
    app=root/'versions'/'new'/'Codexon.exe';app.parent.mkdir(parents=True);app.touch()
    tool=root/'maintenance'/'new'/'CodexonRecovery.exe';tool.parent.mkdir(parents=True);tool.touch()
    shared=tmp_path/'shared.json'
    expected=dict(InstallRoot=str(root),AppPath=str(app),RecoveryPath=str(tool))
    shared.write_text(json.dumps(expected))
    monkeypatch.setattr(installation,'pointer_path',lambda:shared)
    import winreg
    monkeypatch.setattr(winreg,'OpenKey',lambda *a,**k:pytest.fail('A stale registry view must not override the committed pointer'))
    assert installation.installed()==expected
    assert not installation.valid_paths({**expected,'AppPath':str(tmp_path/'other.exe')})


def _run_setup_functions(tmp_path, body):
    """Run the production helpers without registering tasks or installing an app."""
    script = tmp_path / 'setup-check.ps1'
    script.write_text(r'''
param([string]$Source, [string]$Fixture)
$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$tokens = $null
$errors = $null
$parsed = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Get-SetupStagePath','Remove-StagedSetupPayload','Copy-SetupPayload','Complete-SetupRun')) {
    $node = $parsed.Find({param($item)
        $item -is [Management.Automation.Language.FunctionDefinitionAst] -and $item.Name -eq $name
    }, $false)
    if (!$node) { throw ('Missing setup function: '+$name) }
    . ([scriptblock]::Create($node.Extent.Text))
}
$root = Join-Path $Fixture 'setup-runs'
$stage = Join-Path $root ('a'*32)
New-Item -ItemType Directory -Path $stage | Out-Null
$sourceExe = Join-Path $Fixture 'source.exe'
$sourceRequest = Join-Path $Fixture 'original-request.ini'
[IO.File]::WriteAllBytes($sourceExe, [byte[]](1..128))
[IO.File]::WriteAllText($sourceRequest, 'registry=Codexon-QA')
[IO.File]::WriteAllText((Join-Path $stage 'unrelated.txt'), 'preserve')
$hash = (Get-FileHash -LiteralPath $sourceExe -Algorithm SHA256).Hash
''' + body, encoding='utf-8-sig')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run(
        [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-File', str(script), '-Source', str(Path(__file__).resolve().parents[1] / 'installer/native-setup.ps1'),
         '-Fixture', str(tmp_path)],
        capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('failure', ['copy', 'hash'])
def test_failed_setup_copy_preserves_receipts_without_leaving_payload(tmp_path, failure):
    prepare = "$sourceExe += '.missing'" if failure == 'copy' else "$hash = 'wrong'"
    result = _run_setup_functions(tmp_path, prepare + r'''
$failed = $false
try { Copy-SetupPayload $root $stage $sourceExe $hash $Source $sourceRequest }
catch { $failed = $true }
@{failed=$failed; names=@(Get-ChildItem -LiteralPath $stage | Select-Object -ExpandProperty Name)} | ConvertTo-Json
''')
    assert result['failed']
    assert set(result['names']) == {'request.ini', 'unrelated.txt'}
    assert (tmp_path / 'setup-runs' / ('a'*32) / 'request.ini').read_text() == 'registry=Codexon-QA'
    assert (tmp_path / 'source.exe').read_bytes() == bytes(range(1, 129))


@pytest.mark.parametrize('safe,exit_code', [(True, 0), (True, 1001), (False, 1001)])
def test_setup_worker_cleans_only_a_finished_payload(tmp_path, safe, exit_code):
    result = _run_setup_functions(tmp_path, r'''
Copy-SetupPayload $root $stage $sourceExe $hash $Source $sourceRequest
''' + f'Complete-SetupRun $root $stage @{{exit_code={exit_code}}} ${str(safe).lower()}\n' + r'''
@{names=@(Get-ChildItem -LiteralPath $stage | Select-Object -ExpandProperty Name);
  receipt=(Get-Content -LiteralPath (Join-Path $stage 'result.json') -Raw | ConvertFrom-Json)} | ConvertTo-Json
''')
    expected = {'request.ini', 'result.json', 'unrelated.txt'}
    if not safe:
        expected.update({'Codexon-Setup.exe', 'native-setup.ps1'})
    assert set(result['names']) == expected
    assert result['receipt'] == {'exit_code': exit_code, 'payload_cleanup_safe': safe}


def test_setup_cleanup_preserves_foreign_linked_and_open_files(tmp_path):
    result = _run_setup_functions(tmp_path, r'''
Copy-SetupPayload $root $stage $sourceExe $hash $Source $sourceRequest
$foreign = Join-Path $Fixture ('b'*32)
New-Item -ItemType Directory -Path $foreign | Out-Null
Copy-Item -LiteralPath $sourceExe -Destination (Join-Path $foreign 'Codexon-Setup.exe')
$foreignRemoved = Remove-StagedSetupPayload $root $foreign
$junction = Join-Path $root ('c'*32)
New-Item -ItemType Junction -Path $junction -Target $foreign | Out-Null
$linkedRemoved = Remove-StagedSetupPayload $root $junction
$open = [IO.File]::Open((Join-Path $stage 'Codexon-Setup.exe'), 'Open', 'Read', 'Read')
try {
    Complete-SetupRun $root $stage @{exit_code=0} $true
    $openPreserved = Test-Path -LiteralPath (Join-Path $stage 'Codexon-Setup.exe')
} finally { $open.Dispose() }
$retried = Remove-StagedSetupPayload $root $stage
@{foreign_removed=$foreignRemoved; linked_removed=$linkedRemoved;
  foreign_preserved=(Test-Path -LiteralPath (Join-Path $foreign 'Codexon-Setup.exe'));
  open_preserved=$openPreserved; retried=$retried;
  names=@(Get-ChildItem -LiteralPath $stage | Select-Object -ExpandProperty Name)} | ConvertTo-Json
''')
    assert result['foreign_removed'] == result['linked_removed'] == 0
    assert result['foreign_preserved'] and result['open_preserved']
    assert result['retried'] == 128
    assert set(result['names']) == {'request.ini', 'result.json', 'unrelated.txt'}
