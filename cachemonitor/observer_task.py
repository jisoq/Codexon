"""Launch the observer from Windows Task Scheduler, outside the caller's job tree."""
import base64
import hashlib
import json
import os
import subprocess


class ObserverTask:
    def __init__(self, home, role='ModelObserver'):
        self.role=role
        self.name='CacheMonitor-'+role+'-'+hashlib.sha256(str(home).encode()).hexdigest()[:12]
        self.marker='CacheMonitor model observer: '+str(home)

    def call(self, operation, command=None, autostart=False, periodic=False):
        if os.name!='nt':
            raise RuntimeError('독립 프록시 실행은 Windows 작업 스케줄러가 필요합니다')
        payload=base64.b64encode(json.dumps({'name':self.name,'marker':self.marker,'operation':operation,
            'executable':command[0] if command else '',
            'arguments':subprocess.list2cmdline(command[1:]) if command else '',
            'autostart':bool(autostart),'periodic':bool(periodic),
            'restart':3 if self.role in ('ModelObserver','ProxySupervisor','ProxyUpdate') else 0}).encode()).decode()
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
    $login=$false
    foreach($t in $existing.Definition.Triggers){if($t.Type -eq 9 -and $t.Enabled){$login=$true}}
    @{registered=$true;autostart=$login;executable=$a.Path;arguments=$a.Arguments;state=$existing.State} | ConvertTo-Json -Compress
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
$definition=$service.NewTask(0)
$definition.RegistrationInfo.Description=$p.marker
$definition.Settings.Enabled=$true
$definition.Settings.AllowDemandStart=$true
$definition.Settings.ExecutionTimeLimit='PT0S'
if($p.periodic){$definition.Settings.ExecutionTimeLimit='PT45S'}
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
if($p.periodic){
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
    def periodic(self,command):return self.call('configure',command,True,periodic=True)
    def remove(self):return self.call('remove')
    def inspect(self):return self.call('inspect')
    def stop(self):return self.call('stop')
