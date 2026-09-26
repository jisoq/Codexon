"""Compare-and-update installed task definitions without starting or killing tasks."""
import base64
import hashlib
import json
import os
import subprocess


def task_call(request):
    payload=base64.b64encode(json.dumps(request).encode()).decode()
    script=r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
$service=New-Object -ComObject Schedule.Service
$service.Connect()
$folder=$service.GetFolder('\')
if($p.operation -eq 'list'){
    $items=@(foreach($task in $folder.GetTasks(1)){
        $d=$task.Definition
        $actions=@(foreach($a in $d.Actions){
            if($a.Type -eq 0){@{executable=$a.Path;arguments=$a.Arguments;directory=$a.WorkingDirectory}}
        })
        @{name=$task.Name;marker=$d.RegistrationInfo.Description;xml=$d.XmlText;
          running=$task.GetInstances(0).Count;state=$task.State;actions=$actions;count=$d.Actions.Count}
    })
    @{tasks=$items} | ConvertTo-Json -Depth 6 -Compress
    exit 0
}
$task=$folder.GetTask($p.name)
if($task.Definition.XmlText -ne $p.xml -or $task.GetInstances(0).Count -gt 0 -or $task.State -eq 2){throw 'Task changed or is still active'}
if($p.operation -eq 'remove'){
    # Remove the definition (including its triggers); never stop a live instance.
    $folder.DeleteTask($p.name,0)
    if(@($folder.GetTasks(1) | Where-Object {$_.Name -eq $p.name}).Count){throw 'Task removal not confirmed'}
}else{
    $d=$task.Definition
    if($d.Actions.Count -ne 1 -or $d.Actions.Item(1).Type -ne 0){throw 'Unknown task actions'}
    $a=$d.Actions.Item(1)
    $a.Path=$p.executable;$a.Arguments=$p.arguments;$a.WorkingDirectory=$p.directory
    # Ordinary services are owned by AppServices. Keep Enabled, but retire
    # autonomous triggers/restarts inherited from older installations.
    $d.Triggers.Clear()
    [xml]$xml=$d.XmlText
    $restart=$xml.SelectSingleNode("//*[local-name()='RestartOnFailure']")
    if($restart){[void]$restart.ParentNode.RemoveChild($restart)}
    $d.XmlText=$xml.OuterXml
    $expected=$d.XmlText
    [void]$folder.RegisterTaskDefinition($p.name,$d,6,$d.Principal.UserId,$null,3)
    if($folder.GetTask($p.name).Definition.XmlText -ne $expected){throw 'Task update not confirmed'}
}
@{ok=$true} | ConvertTo-Json -Compress
'''.replace('__PAYLOAD__',payload)
    encoded=base64.b64encode(script.encode('utf-16-le')).decode()
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
        capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise RuntimeError('Installation task inspection/update failed')
    return json.loads(result.stdout.decode('utf-8-sig'))


def inventory():
    return task_call({'operation':'list'})['tasks']


def owned_role(task, scopes):
    prefix='CacheMonitor model observer: '
    marker=task.get('marker') or ''
    if not marker.startswith(prefix):return None
    scope=marker[len(prefix):]
    if os.path.normcase(os.path.abspath(scope)) not in {os.path.normcase(os.path.abspath(s)) for s in scopes}:return None
    suffix=hashlib.sha256(scope.encode()).hexdigest()[:12]
    for role in ('UsageCollector','CacheWorker','ProxySupervisor','ModelObserver','CacheObservation',
                 'CacheObservationV2','ConnectionCheck','Desktop','Installer','ProxyUpdate'):
        if task['name']==f'CacheMonitor-{role}-{suffix}':return role
    return None


def change(task, command=None):
    from pathlib import Path
    request=dict(operation='remove' if command is None else 'replace',name=task['name'],xml=task['xml'])
    if command:
        request.update(executable=command[0],arguments=subprocess.list2cmdline(command[1:]),
                       directory=str(Path(command[0]).parent))
    task_call(request)
