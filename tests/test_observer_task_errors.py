import base64
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid
import xml.etree.ElementTree as ET

import pytest

from cachemonitor.observer_task import ObserverTask, TaskSchedulerError, retire_desktop_startups


@pytest.mark.parametrize('stage,code',[('connect','0x80070005'),('root-folder','0x80070003'),('lookup-task','0x80070005'),('register','0x80070005')])
def test_scheduler_failure_preserves_context(monkeypatch,stage,code):
    monkeypatch.setattr('cachemonitor.observer_task.subprocess.run',lambda *a,**k:SimpleNamespace(returncode=1,stdout=json.dumps({'error':{'stage':stage,'hresult':code}}).encode(),stderr=b'private diagnostics'))
    with pytest.raises(TaskSchedulerError) as caught:ObserverTask('synthetic').inspect()
    assert isinstance(caught.value,RuntimeError)
    assert (caught.value.operation,caught.value.stage,caught.value.hresult)==('inspect',stage,code)
    assert 'private' not in str(caught.value)


def _scheduler_command(script, **payload):
    encoded_payload = base64.b64encode(json.dumps(payload).encode()).decode()
    script = r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
$service=New-Object -ComObject Schedule.Service
$service.Connect()
$folder=$service.GetFolder('\')
'''.replace('__PAYLOAD__', encoded_payload) + script
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    return ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]


def _scheduler_powershell(script, **payload):
    result = subprocess.run(_scheduler_command(script, **payload), capture_output=True,
                            timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode('utf-8-sig', errors='replace')
    return json.loads(result.stdout.decode('utf-8-sig'))


_DACL_PERMISSION = r'''
$task=$folder.GetTask($p.name)
try {
    $task.SetSecurityDescriptor($task.GetSecurityDescriptor(4),16)
    @{hresult=$null} | ConvertTo-Json -Compress
} catch {
    $failure=$_.Exception
    while($failure.InnerException){$failure=$failure.InnerException}
    @{hresult=('0x{0:X8}' -f $failure.HResult)} | ConvertTo-Json -Compress
}
'''


@pytest.fixture
def scheduler_tasks(tmp_path):
    if sys.platform != 'win32':
        pytest.skip('Windows Task Scheduler permission regression')
    names = []

    def create(home=None, role='ProxyUpdate', write=True, marker=None, command=None):
        home = str(home or tmp_path / uuid.uuid4().hex)
        task = ObserverTask(home, role=role)
        names.append(task.name)
        command = command or [sys.executable, '-c', 'pass']
        _scheduler_powershell(r'''
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$definition=$service.NewTask(0)
$definition.Principal.UserId=$identity.Name
$definition.Principal.LogonType=3
$definition.Principal.RunLevel=0
$definition.RegistrationInfo.Description=$p.marker
$definition.Settings.Enabled=$true
$definition.Settings.AllowDemandStart=$true
$definition.Settings.DisallowStartIfOnBatteries=$false
$definition.Settings.StopIfGoingOnBatteries=$false
$definition.Settings.RestartCount=3
$definition.Settings.RestartInterval='PT1M'
foreach($type in @(1,9)){
    $trigger=$definition.Triggers.Create($type)
    $trigger.Id=if($type -eq 1){'periodic'}else{'logon'}
    $trigger.StartBoundary=(Get-Date).AddYears(1).ToString('yyyy-MM-ddTHH:mm:ss')
    $trigger.Enabled=$true
    if($type -eq 1){$trigger.Repetition.Interval='PT1M'}else{$trigger.UserId=$identity.Name}
}
$action=$definition.Actions.Create(0)
$action.Path=$p.executable
$action.Arguments=$p.arguments
$action.WorkingDirectory=[IO.Path]::GetDirectoryName($p.executable)
$task=$folder.RegisterTaskDefinition($p.name,$definition,2,$identity.Name,$null,3)
$rights=if($p.write){'GRGWGXSD'}else{'GRGXSD'}
$task.SetSecurityDescriptor("D:P(A;;GR;;;OW)(A;;GA;;;SY)(A;;GA;;;BA)(A;;$rights;;;"+$identity.User.Value+")",16)
@{created=$true} | ConvertTo-Json -Compress
''', name=task.name, marker=marker if marker is not None else task.marker,
            executable=command[0], arguments=subprocess.list2cmdline(command[1:]), write=write)
        permission = _scheduler_powershell(_DACL_PERMISSION, name=task.name)
        if permission['hresult'] is None:
            pytest.skip('Task owner still has WriteDAC; use a non-elevated Windows worker')
        assert permission['hresult'] == '0x80070005', permission
        access = _scheduler_powershell(r'''
$task=$folder.GetTask($p.name)
$definition=$task.Definition
try {
    [void]$folder.RegisterTaskDefinition($p.name,$definition,22,$definition.Principal.UserId,$null,3)
    @{hresult=$null} | ConvertTo-Json -Compress
} catch {
    $failure=$_.Exception
    while($failure.InnerException){$failure=$failure.InnerException}
    @{hresult=('0x{0:X8}' -f $failure.HResult)} | ConvertTo-Json -Compress
}
''', name=task.name)
        assert access['hresult'] == (None if write else '0x80070005'), access
        return task

    try:
        yield create
    finally:
        if names:
            removed = _scheduler_powershell(r'''
$failures=@()
foreach($name in $p.names){
    foreach($task in $folder.GetTasks(1)){
        if($task.Name -ne $name){continue}
        try{$task.Stop(0)}catch{$failures+=($_.Exception.Message)}
        try{$folder.DeleteTask($name,0)}catch{$failures+=($_.Exception.Message)}
    }
}
$remaining=@($folder.GetTasks(1) | Where-Object {$p.names -contains $_.Name} | ForEach-Object {$_.Name})
@{remaining=$remaining;failures=$failures} | ConvertTo-Json -Compress
''', names=names)
            assert removed == {'remaining': [], 'failures': []}, removed


def _task_snapshot(task):
    result = _scheduler_powershell(r'''
$task=$folder.GetTask($p.name)
@{xml=$task.Definition.XmlText;sddl=$task.GetSecurityDescriptor(7)} | ConvertTo-Json -Compress
''', name=task.name)
    definition = ET.fromstring(result['xml'])
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    triggers = definition.find('t:Triggers', ns)
    return {
        'enabled': definition.findtext('t:Settings/t:Enabled', namespaces=ns),
        'triggers': {trigger.attrib['id']: trigger.findtext('t:Enabled', namespaces=ns)
                     for trigger in triggers},
        'restart': definition.find('t:Settings/t:RestartOnFailure', ns) is not None,
        'principal': ET.tostring(definition.find('t:Principals', ns)),
        'actions': ET.tostring(definition.find('t:Actions', ns)),
        'marker': definition.findtext('t:RegistrationInfo/t:Description', namespaces=ns),
        'sddl': result['sddl'],
    }


@pytest.mark.integration
def test_finish_update_without_write_dac_preserves_other_task_state(scheduler_tasks):
    task = scheduler_tasks()
    before = _task_snapshot(task)
    assert before['enabled'] == 'true'
    assert before['triggers'] == {'periodic': 'true', 'logon': 'true'}
    expected = {**before, 'triggers': {'periodic': 'false', 'logon': 'true'}}
    assert task.finish_update() == {'watchdog': False}
    assert _task_snapshot(task) == expected
    assert task.finish_update() == {'watchdog': False}
    assert _task_snapshot(task) == expected


@pytest.mark.integration
def test_scheduled_finish_update_without_write_dac(scheduler_tasks, tmp_path):
    home = str(tmp_path / uuid.uuid4().hex)
    task = ObserverTask(home, role='ProxyUpdate')
    output = tmp_path / 'scheduled-result.json'
    child = tmp_path / 'finish_update.py'
    permission_command = _scheduler_command(_DACL_PERMISSION, name=task.name)
    child.write_text(f'''
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from cachemonitor.observer_task import ObserverTask, TaskSchedulerError
permission = subprocess.run({permission_command!r}, capture_output=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
result = {{'permission_returncode': permission.returncode,
           'permission': permission.stdout.decode('utf-8-sig')}}
try:
    result['result'] = ObserverTask({home!r}, role='ProxyUpdate').finish_update()
except TaskSchedulerError as error:
    result['error'] = [error.operation, error.stage, error.hresult]
output = Path({str(output)!r})
pending = output.with_suffix('.tmp')
pending.write_text(json.dumps(result), encoding='utf-8')
pending.replace(output)
''', encoding='utf-8')
    task = scheduler_tasks(home=home, command=[sys.executable, str(child)])
    before = _task_snapshot(task)
    _scheduler_powershell(r'''
[void]$folder.GetTask($p.name).Run($null)
@{started=$true} | ConvertTo-Json -Compress
''', name=task.name)
    deadline = time.monotonic() + 40
    while not output.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert output.exists(), task.inspect()
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result['permission_returncode'] == 0, result
    assert json.loads(result['permission']) == {'hresult': '0x80070005'}, result
    assert result.get('result') == {'watchdog': False}, result
    assert _task_snapshot(task) == {**before, 'triggers': {'periodic': 'false', 'logon': 'true'}}


@pytest.mark.integration
def test_suspend_without_write_dac_disables_task_triggers_and_restart(scheduler_tasks):
    task = scheduler_tasks(role='ConnectionCheck')
    before = _task_snapshot(task)
    assert before['restart']
    expected = {**before, 'enabled': 'false', 'restart': False,
                'triggers': {'periodic': 'false', 'logon': 'false'}}
    assert task.suspend() == {'suspended': True}
    assert _task_snapshot(task) == expected
    assert task.suspend() == {'suspended': True}
    assert _task_snapshot(task) == expected


@pytest.mark.integration
def test_retire_startups_without_write_dac_changes_only_owned_logon_triggers(scheduler_tasks, tmp_path):
    root = tmp_path / uuid.uuid4().hex
    exe = root / 'versions' / 'old' / 'Codexon.exe'
    outside = tmp_path / uuid.uuid4().hex / 'versions' / 'old' / 'Codexon.exe'
    owned = scheduler_tasks(home=exe, role='Desktop', command=[str(exe), '--hidden'])
    unmarked = scheduler_tasks(home=exe.parent / 'unmarked' / 'Codexon.exe', role='Desktop',
                              marker='Unrelated application', command=[str(exe), '--hidden'])
    other = scheduler_tasks(home=outside, role='Desktop', command=[str(outside), '--hidden'])
    worker = scheduler_tasks(home=exe, role='CacheWorker', command=[str(exe), '--hidden'])
    before = {task.name: _task_snapshot(task) for task in (owned, unmarked, other, worker)}
    assert retire_desktop_startups(root) == {'retired': [owned.name]}
    assert _task_snapshot(owned) == {**before[owned.name],
                                    'triggers': {'periodic': 'true', 'logon': 'false'}}
    for task in (unmarked, other, worker):
        assert _task_snapshot(task) == before[task.name]
    assert retire_desktop_startups(root) == {'retired': []}


@pytest.mark.integration
@pytest.mark.parametrize('operation', ['finish_update', 'suspend'])
def test_definition_updates_still_report_denied_task_write(scheduler_tasks, operation):
    task = scheduler_tasks(write=False)
    before = _task_snapshot(task)
    with pytest.raises(TaskSchedulerError) as caught:
        getattr(task, operation)()
    assert (caught.value.operation, caught.value.stage, caught.value.hresult) == (
        operation, operation, '0x80070005')
    assert _task_snapshot(task) == before
