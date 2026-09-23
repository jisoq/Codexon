param(
    [Parameter(Mandatory=$true)][string]$Executable,
    [string]$TaskName = 'CacheMonitor-Application',
    [string]$Arguments = ''
)
$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $Executable).Path
if ([IO.Path]::GetFileName($exe) -ne 'Codexon.exe') { throw 'Expected the installed Codexon executable.' }
$task = Get-ScheduledTask -TaskName $TaskName
# A Task Scheduler-created executable can receive a process/pipe security context
# that prevents frozen multiprocessing from duplicating its bootstrap pipe.
# Launch through an ordinary child process without elevation or ACL changes.
$quotedExe = $exe.Replace("'", "''")
$quotedDirectory = (Split-Path $exe).Replace("'", "''")
$argumentOption = if ($Arguments) { " -ArgumentList '" + $Arguments.Replace("'", "''") + "'" } else { '' }
$script = "`$ErrorActionPreference='Stop'; `$app=Start-Process -FilePath '$quotedExe' -WorkingDirectory '$quotedDirectory'$argumentOption -WindowStyle Hidden -PassThru; `$app.WaitForExit(); exit `$app.ExitCode"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
$hostExe = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$action = New-ScheduledTaskAction -Execute $hostExe -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $encoded" -WorkingDirectory (Split-Path $exe)
Set-ScheduledTask -TaskName $TaskName -Action $action | Out-Null
Write-Output "Configured $TaskName for $exe"
