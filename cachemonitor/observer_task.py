"""Launch the observer from Windows Task Scheduler, outside the caller's job tree."""
import base64
import hashlib
import json
import os
import subprocess


def retire_desktop_startups(root):
    """Installer handoff tasks must not compete with the app's Run preference.

    Disable only logon triggers of owned GUI tasks in this installation. Keep
    definitions, history, running processes and every proxy task untouched.
    """
    from pathlib import Path
    payload=base64.b64encode(str(Path(root).resolve()/'versions').encode()).decode()
    script=r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$root=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ROOT__')).TrimEnd('\')+'\'
$service=New-Object -ComObject Schedule.Service
$service.Connect()
$folder=$service.GetFolder('\')
$changed=@()
foreach($task in $folder.GetTasks(1)){
    if(!$task.Name.StartsWith('CacheMonitor-Desktop-')){continue}
    $definition=$task.Definition
    if($definition.Actions.Count -ne 1){continue}
    $action=$definition.Actions.Item(1)
    $path=[IO.Path]::GetFullPath($action.Path)
    if(!$path.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($path) -ne 'Codexon.exe'){continue}
    if($definition.RegistrationInfo.Description -ne ('CacheMonitor model observer: '+$action.Path)){continue}
    $update=$false
    foreach($trigger in $definition.Triggers){if($trigger.Type -eq 9 -and $trigger.Enabled){$trigger.Enabled=$false;$update=$true}}
    if($update){
        [void]$folder.RegisterTaskDefinition($task.Name,$definition,6,$definition.Principal.UserId,$null,3)
        $changed+=$task.Name
    }
}
@{retired=$changed} | ConvertTo-Json -Compress
'''.replace('__ROOT__',payload)
    encoded=base64.b64encode(script.encode('utf-16-le')).decode()
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
        capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise RuntimeError('이전 GUI의 중복 자동 시작을 정리하지 못했습니다.')
    return json.loads(result.stdout.decode('utf-8-sig'))


def remove_installation_collectors(root):
    """Remove owned collector definitions, including exhausted or stopped tasks."""
    from pathlib import Path
    if os.name!='nt':return {'removed':[]}
    payload=base64.b64encode(str(Path(root).resolve()).encode()).decode()
    script=r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$root=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ROOT__')).TrimEnd('\')+'\'
$service=New-Object -ComObject Schedule.Service
$service.Connect()
$folder=$service.GetFolder('\')
$removed=@()
$prefix='CacheMonitor model observer: '
foreach($task in $folder.GetTasks(1)){
    if(!$task.Name.StartsWith('CacheMonitor-UsageCollector-')){continue}
    $definition=$task.Definition
    if($definition.Actions.Count -ne 1){continue}
    $action=$definition.Actions.Item(1)
    if(![IO.Path]::IsPathRooted($action.Path)){continue}
    $path=[IO.Path]::GetFullPath($action.Path)
    if(!$path.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($path) -ne 'Codexon.exe'){continue}
    if($action.Arguments -notmatch '(^|\s)--usage-collector(\s|$)'){continue}
    $description=$definition.RegistrationInfo.Description
    if(!$description.StartsWith($prefix)){continue}
    $scope=$description.Substring($prefix.Length)
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$hash=($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($scope)) | ForEach-Object {$_.ToString('x2')}) -join ''}finally{$sha.Dispose()}
    if($task.Name -ne ('CacheMonitor-UsageCollector-'+$hash.Substring(0,12))){continue}
    $task.Stop(0)
    $folder.DeleteTask($task.Name,0)
    $removed+=$task.Name
}
@{removed=$removed} | ConvertTo-Json -Compress
'''.replace('__ROOT__',payload)
    encoded=base64.b64encode(script.encode('utf-16-le')).decode()
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
        capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise RuntimeError('설치된 백그라운드 수집기 작업을 정리하지 못했습니다.')
    return json.loads(result.stdout.decode('utf-8-sig'))


class ObserverTask:
    def __init__(self, home, role='ModelObserver'):
        self.role=role
        self.name='CacheMonitor-'+role+'-'+hashlib.sha256(str(home).encode()).hexdigest()[:12]
        self.marker='CacheMonitor model observer: '+str(home)

    def call(self, operation, command=None, autostart=False):
        if os.name!='nt':
            raise RuntimeError('독립 프록시 실행은 Windows 작업 스케줄러가 필요합니다')
        payload=base64.b64encode(json.dumps({'name':self.name,'marker':self.marker,'operation':operation,
            'executable':command[0] if command else '',
            'arguments':subprocess.list2cmdline(command[1:]) if command else '',
            'autostart':bool(autostart) if self.role not in ('ModelObserver','ProxySupervisor','CacheObservation','CacheObservationV2','CacheWorker','UsageCollector') else False,
            'update_watchdog':self.role=='ProxyUpdate',
            'restart':3 if self.role=='ProxyUpdate' else 0}).encode()).decode()
        script=r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
$service=New-Object -ComObject Schedule.Service
$service.Connect()
$folder=$service.GetFolder('\')
$existing=$null
foreach($candidate in $folder.GetTasks(1)){if($candidate.Name -eq $p.name){$existing=$candidate;break}}
if($existing -and $existing.Definition.RegistrationInfo.Description -ne $p.marker){throw 'Task name is owned by another application'}
if($p.operation -eq 'inspect'){
    if(!$existing){@{registered=$false;autostart=$false} | ConvertTo-Json -Compress;exit 0}
    $a=$existing.Definition.Actions.Item(1)
    $login=$false;$periodic=$false
    foreach($t in $existing.Definition.Triggers){if($t.Type -eq 9 -and $t.Enabled){$login=$true}}
    foreach($t in $existing.Definition.Triggers){if($t.Type -eq 1 -and $t.Enabled){$periodic=$true}}
    @{registered=$true;autostart=$login;periodic=$periodic;enabled=$existing.Enabled;running=$existing.GetInstances(0).Count;execution_limit=$existing.Definition.Settings.ExecutionTimeLimit;executable=$a.Path;arguments=$a.Arguments;state=$existing.State;last_result=$existing.LastTaskResult;restartCount=$existing.Definition.Settings.RestartCount} | ConvertTo-Json -Compress
    exit 0
}
if($p.operation -eq 'remove'){
    if($existing){$folder.DeleteTask($p.name,0)}
    @{registered=$false} | ConvertTo-Json -Compress
    exit 0
}
if($p.operation -eq 'stop'){
    if($existing){$existing.Stop(0)}
    @{stopped=$true} | ConvertTo-Json -Compress
    exit 0
}
if($p.operation -eq 'suspend'){
    if($existing){
        $definition=$existing.Definition
        $definition.Settings.Enabled=$false
        foreach($trigger in $definition.Triggers){$trigger.Enabled=$false}
        # A present RestartOnFailure element cannot contain Count=0.
        [xml]$xml=$definition.XmlText
        $restart=$xml.SelectSingleNode("//*[local-name()='RestartOnFailure']")
        if($restart){[void]$restart.ParentNode.RemoveChild($restart)}
        $definition.XmlText=$xml.OuterXml
        [void]$folder.RegisterTaskDefinition($p.name,$definition,6,$definition.Principal.UserId,$null,3)
    }
    @{suspended=$true} | ConvertTo-Json -Compress
    exit 0
}
if($p.operation -eq 'finish_update'){
    if($existing){
        $definition=$existing.Definition
        foreach($trigger in $definition.Triggers){if($trigger.Type -eq 1){$trigger.Enabled=$false}}
        [void]$folder.RegisterTaskDefinition($p.name,$definition,6,$definition.Principal.UserId,$null,3)
    }
    @{watchdog=$false} | ConvertTo-Json -Compress
    exit 0
}
$definition=$service.NewTask(0)
$definition.RegistrationInfo.Description=$p.marker
$definition.Settings.Enabled=$true
$definition.Settings.AllowDemandStart=$true
$definition.Settings.ExecutionTimeLimit='PT0S'
$definition.Settings.MultipleInstances=2
$definition.Settings.DisallowStartIfOnBatteries=$false
$definition.Settings.StopIfGoingOnBatteries=$false
$definition.Settings.StartWhenAvailable=$true
if($p.restart -gt 0){$definition.Settings.RestartCount=$p.restart;$definition.Settings.RestartInterval='PT1M'}
$definition.Principal.LogonType=3
$definition.Principal.RunLevel=0
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$definition.Principal.UserId=$user
if($p.autostart){$trigger=$definition.Triggers.Create(9);$trigger.UserId=$user;$trigger.Enabled=$true}
if($p.update_watchdog){
    $trigger=$definition.Triggers.Create(1)
    $trigger.StartBoundary=(Get-Date).AddSeconds(10).ToString('yyyy-MM-ddTHH:mm:ss')
    $trigger.Repetition.Interval='PT1M'
    $trigger.Enabled=$true
}
$action=$definition.Actions.Create(0)
$action.Path=$p.executable
$action.Arguments=$p.arguments
$action.WorkingDirectory=[IO.Path]::GetDirectoryName($p.executable)
$registered=$folder.RegisterTaskDefinition($p.name,$definition,6,$user,$null,3)
if($p.operation -eq 'run'){[void]$registered.Run($null)}
@{registered=$true;autostart=$p.autostart} | ConvertTo-Json -Compress
'''.replace('__PAYLOAD__',payload)
        encoded=base64.b64encode(script.encode('utf-16-le')).decode()
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
            capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('독립 프록시 작업을 등록하지 못했습니다. Codex 연결 설정은 변경하지 않습니다.')
        return json.loads(result.stdout.decode('utf-8-sig'))

    def start(self,command,autostart=False):return self.call('run',command,autostart)
    def configure(self,command,autostart):return self.call('configure',command,autostart)
    def remove(self):return self.call('remove')
    def inspect(self):return self.call('inspect')
    def stop(self):return self.call('stop')
    def suspend(self):return self.call('suspend')
    def finish_update(self):return self.call('finish_update')
