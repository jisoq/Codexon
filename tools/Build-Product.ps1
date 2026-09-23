param(
    [ValidateSet('Codexon')][string]$Product,
    [string]$DistPath,
    [string]$WorkPath,
    [string]$PythonPath = ''
)
$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$sourceSha = (& git -C $sourceRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceSha -notmatch '^[0-9a-f]{40}$') { throw 'A source commit is required for release packaging.' }
if (@(& git -C $sourceRoot status --porcelain --untracked-files=normal).Count -ne 0) { throw 'Commit source changes before release packaging.' }
function Absolute-Output([string]$Value) {
    if ([IO.Path]::IsPathRooted($Value)) { return [IO.Path]::GetFullPath($Value) }
    return [IO.Path]::GetFullPath((Join-Path $sourceRoot $Value))
}
$distRoot = Absolute-Output $DistPath
$workRoot = Absolute-Output $WorkPath
$target = Join-Path $distRoot $Product
$executable = Join-Path $target ($Product + '.exe')
$active = @(Get-CimInstance Win32_Process -Filter "Name='$Product.exe'")
if ($active | Where-Object { $_.ExecutablePath -eq $executable }) {
    throw 'The output is running. Choose a separate DistPath to preserve the app and proxy.'
}
if (!$PythonPath) {
    $PythonPath = Join-Path $sourceRoot '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $PythonPath)) { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
}
$stamp = Get-Date -Format 'yyyy-MM-dd/HHmmss-fff'
$archiveRoot = Join-Path $sourceRoot ('_archive/builds/' + $stamp + '/' + $Product)
$stagingRoot = Join-Path $sourceRoot ('.staging/' + $Product)
function Archive-Directory([string]$Path, [string]$AllowedRoot, [string]$Label) {
    $absolute = [IO.Path]::GetFullPath($Path)
    $allowed = [IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\') + '\'
    if (!$absolute.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe archive source: $absolute" }
    if (!(Test-Path -LiteralPath $absolute)) { return }
    if ((Get-Item -LiteralPath $absolute).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked output must not be moved: $absolute" }
    $destination = [IO.Path]::GetFullPath((Join-Path $archiveRoot $Label))
    if (!$destination.StartsWith([IO.Path]::GetFullPath($archiveRoot) + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe archive destination' }
    New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
    Move-Item -LiteralPath $absolute -Destination $destination
}
Archive-Directory $stagingRoot (Join-Path $sourceRoot '.staging') 'previous-staging'
Archive-Directory (Join-Path $workRoot $Product) $workRoot 'previous-build'
Push-Location $sourceRoot
try {
    & $PythonPath -m PyInstaller --noconfirm --clean --distpath $stagingRoot --workpath $workRoot ($Product + '.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Build failed; the previous distribution remains in place.' }
    $stagedProduct = Join-Path $stagingRoot $Product
    $stagedExe = Join-Path $stagedProduct ($Product + '.exe')
    if (!(Test-Path -LiteralPath $stagedExe)) { throw 'Build did not produce an executable.' }
    # Recheck after compilation: a user may have opened the prior output meanwhile.
    if (Get-CimInstance Win32_Process -Filter "Name='$Product.exe'" | Where-Object { $_.ExecutablePath -eq $executable }) {
        throw 'Output started during compilation. The verified build remains in staging.'
    }
    $checked = [IO.Path]::GetFullPath($stagedProduct)
    if (!$checked.StartsWith([IO.Path]::GetFullPath($stagingRoot) + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe staged output' }
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'LICENSE') -Destination (Join-Path $stagedProduct 'LICENSE')
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'THIRD-PARTY-NOTICES.md') -Destination (Join-Path $stagedProduct 'THIRD-PARTY-NOTICES.md')
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'SOURCE-OFFER.md') -Destination (Join-Path $stagedProduct 'SOURCE-OFFER.md')
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/user-guide.md') -Destination (Join-Path $stagedProduct 'USER-GUIDE.md')
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/user-guide.ko.md') -Destination (Join-Path $stagedProduct 'USER-GUIDE.ko.md')
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'LICENSES') -Destination (Join-Path $stagedProduct 'LICENSES') -Recurse
    & $PythonPath (Join-Path $sourceRoot 'tools/collect_notices.py') --toc (Join-Path $workRoot "$Product/PYZ-00.toc") --product $stagedProduct
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare dependency notices.' }
    [pscustomobject]@{product=$Product;version=([regex]::Match((Get-Content -LiteralPath (Join-Path $sourceRoot 'cachemonitor/version.py') -Raw),'[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+')).Value;commit=$sourceSha;architecture=[System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant();executable=($Product + '.exe');sha256=(Get-FileHash -LiteralPath $stagedExe -Algorithm SHA256).Hash.ToLowerInvariant()} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stagedProduct 'build-manifest.json') -Encoding utf8
    & $PythonPath (Join-Path $sourceRoot 'tools/package_release.py') --product $stagedProduct --validate-only
    if ($LASTEXITCODE -ne 0) { throw 'Public distribution validation failed.' }
    Archive-Directory $target $distRoot 'previous-dist'
    New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
    Move-Item -LiteralPath $checked -Destination $target
    Write-Output $executable
} finally { Pop-Location }
