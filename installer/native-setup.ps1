param([Parameter(Mandatory)][string]$Request, [switch]$Worker)
$ErrorActionPreference = 'Stop'
# A caller running PowerShell 7 can pass an incompatible PSModulePath to 5.1.
# The installer uses only Windows' built-in modules, never caller profile modules.
$env:PSModulePath = Join-Path $PSHOME 'Modules'
trap {
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        @{exit_code=1001; error=$_.Exception.Message} | ConvertTo-Json |
            Set-Content -LiteralPath (Join-Path $stage 'bootstrap-error.json') -Encoding utf8
    }
    Write-Error $_ -ErrorAction Continue
    exit 1001
}
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class SetupEnvironment {
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] public static extern uint GetPrivateProfileString(string section,string key,string fallback,StringBuilder value,uint size,string path);
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] public static extern int GetCurrentPackageFullName(ref uint size,IntPtr name);
}
'@
function Read-Request([string]$Key) {
    $value = New-Object Text.StringBuilder 32768
    [void][SetupEnvironment]::GetPrivateProfileString('request',$Key,'',$value,32768,$Request)
    return $value.ToString()
}
function Quote-Argument([string]$Value) {
    return '"' + ($Value -replace '(\\*)"','$1$1\"' -replace '(\\+)$','$1$1') + '"'
}
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('\')
if (!$Worker) {
    $run = [guid]::NewGuid().ToString('N')
    $stage = Join-Path $env:USERPROFILE ('.cachemonitor\setup-runs\'+$run)
    New-Item -ItemType Directory -Path $stage | Out-Null
    $source = Read-Request 'source'
    $expected = Read-Request 'sha256'
    $setup = Join-Path $stage 'Codexon-Setup.exe'
    Copy-Item -LiteralPath $source -Destination $setup
    if ((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash -ne $expected) { throw 'Installer staging hash mismatch' }
    Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $stage 'native-setup.ps1')
    Copy-Item -LiteralPath $Request -Destination (Join-Path $stage 'request.ini')
    $Request = Join-Path $stage 'request.ini'
    $taskName = 'Codexon-Setup-'+$run
    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = 'Codexon installation '+$run
    $definition.Settings.ExecutionTimeLimit = 'PT0S'
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Principal.LogonType = 3
    $definition.Principal.RunLevel = 0
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $definition.Principal.UserId = $user
    $action = $definition.Actions.Create(0)
    $action.Path = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File '+(Quote-Argument (Join-Path $stage 'native-setup.ps1'))+' -Worker -Request '+(Quote-Argument $Request)
    $action.WorkingDirectory = $stage
    $task = $folder.RegisterTaskDefinition($taskName,$definition,6,$user,$null,3)
    [void]$task.Run($null)
    $deadline = (Get-Date).AddMinutes(30)
    $report = Join-Path $stage 'result.json'
    while ((Get-Date) -lt $deadline) {
        $task = $folder.GetTask($taskName)
        if ($task.State -ne 4 -and $task.State -ne 2 -and $task.LastTaskResult -ne 267011) {
            $exitCode = [int]$task.LastTaskResult
            $folder.DeleteTask($taskName,0)
            if (!(Test-Path -LiteralPath $report)) { throw 'Installer exited without its result' }
            $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
            if ($result.exit_code -ne $exitCode) { throw 'Installer result does not match task exit status' }
            # Keep the small receipt, not an accumulating second copy of Setup.
            Remove-Item -LiteralPath $setup -Force
            Remove-Item -LiteralPath (Join-Path $stage 'native-setup.ps1') -Force
            exit $exitCode
        }
        Start-Sleep -Milliseconds 500
    }
    throw 'Installation is still pending; its task and files have been preserved'
}

$exitCode = 1001
$result = @{}
$mutex = $null
$ownsMutex = $false
try {
    [uint32]$size = 0
    if ([SetupEnvironment]::GetCurrentPackageFullName([ref]$size,[IntPtr]::Zero) -ne 15700) { throw 'Native Windows environment was not established' }
    $registry = Read-Request 'registry'
    if ($registry -notin @('Codexon','Codexon-QA')) { throw 'Unknown installation identity' }
    $mutex = New-Object Threading.Mutex $false,('Local\'+$registry+'.NativeInstaller')
    $ownsMutex = $mutex.WaitOne(0)
    if (!$ownsMutex) { throw 'Another installation is already running' }
    $uninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\'+$registry+'_is1'
    $previousMetadata = @{}
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($uninstallKey)
    if ($key) {
        try {
            foreach ($name in @('DisplayIcon','DisplayVersion','UninstallString','QuietUninstallString','InstallLocation')) {
                if ($key.GetValueNames() -contains $name) { $previousMetadata[$name] = $key.GetValue($name) }
            }
        } finally { $key.Dispose() }
    }
    $stage = Split-Path -Parent $Request
    $setup = Join-Path $stage 'Codexon-Setup.exe'
    if ((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash -ne (Read-Request 'sha256')) { throw 'Native installer hash mismatch' }
    # Only the scheduled worker creates this proof, after verifying the payload.
    Set-Content -LiteralPath ($Request+'.ready') -Value (Read-Request 'sha256') -Encoding ascii
    $arguments = @()
    for ($i=0; $i -lt [int](Read-Request 'count'); $i++) {
        $argument = Read-Request ('arg'+$i)
        # The bootstrap still has the requested log open while it waits.
        if ($argument.StartsWith('/LOG=',[StringComparison]::OrdinalIgnoreCase)) { $argument += '.native.log' }
        $arguments += Quote-Argument $argument
    }
    $arguments += Quote-Argument ('/CODEXONREQUEST='+$Request)
    $process = Start-Process -FilePath $setup -ArgumentList $arguments -PassThru -Wait
    $exitCode = $process.ExitCode
    # Inno writes its uninstall metadata before the activation transaction.
    # A failed upgrade must not leave its icon/version pointing at that payload.
    if ($exitCode -ne 0 -and $previousMetadata.Count) {
        $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($uninstallKey)
        try {
            foreach ($name in $previousMetadata.Keys) {
                $key.SetValue($name,$previousMetadata[$name],[Microsoft.Win32.RegistryValueKind]::String)
                if ($key.GetValue($name) -ne $previousMetadata[$name]) { throw 'Uninstall metadata rollback failed' }
            }
        } finally { $key.Dispose() }
    }
    $result.exit_code = $exitCode
} catch {
    $result.error = $_.Exception.Message
    $result.exit_code = 1001
    $exitCode = 1001
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
    $result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path (Split-Path -Parent $Request) 'result.json') -Encoding utf8
}
exit $exitCode
