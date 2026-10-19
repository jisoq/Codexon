[CmdletBinding()]
param(
    [ValidateSet('multiagent', 'active', 'idle', 'loading', 'error', 'mismatch', 'unknown', 'cache-miss', 'slow')]
    [string]$Scenario = 'multiagent',
    [ValidateSet('dark', 'light')][string]$Theme = 'dark',
    [ValidateSet('ko', 'en')][string]$Language = 'ko',
    [switch]$Compact,
    [switch]$Snapshot,
    [string]$SnapshotPath,
    [string]$Session,
    [string]$Verify
)
$ErrorActionPreference = 'Stop'
$previewRoot = Split-Path -Parent $PSScriptRoot
$previewPython = Join-Path $previewRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $previewPython -PathType Leaf)) {
    throw "Preview requires .venv built from requirements-release.lock. Run: py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements-release.lock"
}
Push-Location -LiteralPath $previewRoot
try {
    & $previewPython -B (Join-Path $PSScriptRoot 'check_environment.py')
    if ($LASTEXITCODE -ne 0) {
        throw 'Environment differs from requirements-release.lock. Update explicitly: .\.venv\Scripts\python.exe -m pip install -r requirements-release.lock'
    }
    $previewArguments = @('-B', (Join-Path $PSScriptRoot 'preview_overlay.py'), '--scenario', $Scenario, '--theme', $Theme, '--language', $Language)
    if ($Compact) { $previewArguments += '--compact' }
    if ($SnapshotPath) { $previewArguments += @('--snapshot', $SnapshotPath) }
    elseif ($Snapshot) { $previewArguments += '--snapshot' }
    if ($Session) { $previewArguments += @('--session', $Session) }
    if ($Verify) { $previewArguments += @('--verify', $Verify) }
    & $previewPython @previewArguments
    $previewExitCode = $LASTEXITCODE
}
finally { Pop-Location }
exit $previewExitCode
