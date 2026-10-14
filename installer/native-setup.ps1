param([Parameter(Mandatory)][string]$Request, [switch]$Worker)
$ErrorActionPreference = 'Stop'
# A caller running PowerShell 7 can pass an incompatible PSModulePath to 5.1.
# The installer uses only Windows' built-in modules, never caller profile modules.
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$setupRoot = Join-Path $env:USERPROFILE '.cachemonitor\setup-runs'
$bootstrapCanCleanup = $false
trap {
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        if ($bootstrapCanCleanup) { [void](Remove-StagedSetupPayload $setupRoot $stage) }
        @{exit_code=1001; error=$_.Exception.Message} | ConvertTo-Json |
            Set-Content -LiteralPath (Join-Path $stage 'bootstrap-error.json') -Encoding utf8
    }
    Write-Error $_ -ErrorAction Continue
    exit 1001
}
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class SetupEnvironment {
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] public static extern int GetCurrentPackageFullName(ref uint size,IntPtr name);
}
'@
function Read-Request([string]$Key) {
    foreach ($line in [IO.File]::ReadAllLines($Request,[Text.Encoding]::UTF8)) {
        if ($line.StartsWith($Key+'=',[StringComparison]::Ordinal)) { return $line.Substring($Key.Length+1) }
    }
    throw ('Missing installer request field: '+$Key)
}
function Quote-Argument([string]$Value) {
    return '"' + ($Value -replace '(\\*)"','$1$1\"' -replace '(\\+)$','$1$1') + '"'
}
function Get-SetupStagePath([string]$Root, [string]$Directory) {
    try {
        $base = [IO.Path]::GetFullPath($Root).TrimEnd('\')
        $selected = [IO.Path]::GetFullPath($Directory).TrimEnd('\')
        if ([IO.Path]::GetDirectoryName($selected) -ne $base -or
            [IO.Path]::GetFileName($selected) -notmatch '^[0-9a-f]{32}$') { return $null }
        $item = Get-Item -LiteralPath $selected -Force -ErrorAction Stop
        if (!$item.PSIsContainer) { return $null }
        for ($parent = $item; $null -ne $parent; $parent = $parent.Parent) {
            if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $null }
        }
        return $selected
    } catch { return $null }
}
function Remove-StagedSetupPayload([string]$Root, [string]$Directory) {
    # Only the completed owner calls this; receipts and unknown files stay intact.
    $removed = 0L
    foreach ($name in @('Codexon-Setup.exe','native-setup.ps1')) {
        $selected = Get-SetupStagePath $Root $Directory
        if (!$selected) { return $removed }
        $file = Join-Path $selected $name
        try {
            $item = Get-Item -LiteralPath $file -Force -ErrorAction Stop
            if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { continue }
            $bytes = $item.Length
            Remove-Item -LiteralPath $file -Force -ErrorAction Stop
            $removed += $bytes
        } catch { } # A locked cache file must not change the installation result.
    }
    return $removed
}
function Copy-SetupPayload([string]$Root, [string]$Directory, [string]$Source,
                           [string]$Expected, [string]$Helper, [string]$RequestFile) {
    try {
        # Record ownership before the large copy, including interrupted transfers.
        Copy-Item -LiteralPath $RequestFile -Destination (Join-Path $Directory 'request.ini')
        Copy-Item -LiteralPath $Helper -Destination (Join-Path $Directory 'native-setup.ps1')
        $output = Join-Path $Directory 'Codexon-Setup.exe'
        Copy-Item -LiteralPath $Source -Destination $output
        if ((Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash -ne $Expected) { throw 'Installer staging hash mismatch' }
    } catch {
        [void](Remove-StagedSetupPayload $Root $Directory)
        throw
    }
}
function Complete-SetupRun([string]$Root, [string]$Directory, [hashtable]$Result, [bool]$CleanupSafe) {
    $Result.payload_cleanup_safe = $CleanupSafe
    $Result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Directory 'result.json') -Encoding utf8
    if ($CleanupSafe) { [void](Remove-StagedSetupPayload $Root $Directory) }
}
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('\')
if (!$Worker) {
    $run = [guid]::NewGuid().ToString('N')
    $stage = Join-Path $setupRoot $run
    New-Item -ItemType Directory -Path $stage | Out-Null
    $bootstrapCanCleanup = $true
    $source = Read-Request 'source'
    $expected = Read-Request 'sha256'
    $setup = Join-Path $stage 'Codexon-Setup.exe'
    Copy-SetupPayload $setupRoot $stage $source $expected $PSCommandPath $Request
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
    # A failed Run call may still have dispatched the worker. Leave its files.
    $bootstrapCanCleanup = $false
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
            if ($result.payload_cleanup_safe) { [void](Remove-StagedSetupPayload $setupRoot $stage) }
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
$installerAttempted = $false
$installerExited = $false
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
    $installerAttempted = $true
    $process = Start-Process -FilePath $setup -ArgumentList $arguments -PassThru -Wait
    $installerExited = $true
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
    Complete-SetupRun $setupRoot (Split-Path -Parent $Request) $result (!$installerAttempted -or $installerExited)
}
exit $exitCode
