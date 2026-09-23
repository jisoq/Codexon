param([Parameter(Mandatory)][string]$ProductDir,
      [Parameter(Mandatory)][string]$OutputDir,
      [string]$Compiler = 'ISCC.exe',
      [switch]$Isolated)
$ErrorActionPreference = 'Stop'
$product = (Resolve-Path -LiteralPath $ProductDir).Path
$manifest = Get-Content -LiteralPath (Join-Path $product 'build-manifest.json') -Raw | ConvertFrom-Json
if ($manifest.product -ne 'Codexon' -or !$manifest.recovery_sha256) { throw 'A verified product with recovery is required.' }
foreach ($entry in @(@('Codexon.exe', $manifest.sha256), @('CodexonRecovery.exe', $manifest.recovery_sha256))) {
    if ((Get-FileHash -LiteralPath (Join-Path $product $entry[0]) -Algorithm SHA256).Hash -ne $entry[1]) { throw 'Product hash mismatch.' }
}
$output = [IO.Path]::GetFullPath($OutputDir)
if (Test-Path -LiteralPath (Join-Path $output 'Codexon-Setup.exe')) { throw 'Use a new versioned output directory.' }
$arguments = @("/DProductDir=$product", "/DProductVersion=$($manifest.version)", "/DOutputDir=$output")
if ($Isolated) { $arguments += '/DIsolated' }
& $Compiler @arguments (Join-Path $PSScriptRoot '../installer/Codexon.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
$setup = Join-Path $output 'Codexon-Setup.exe'
$digest = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($setup + '.sha256') -Value "$digest  Codexon-Setup.exe" -Encoding ascii
Write-Output $setup
