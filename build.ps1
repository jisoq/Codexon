param([string]$DistPath = 'dist', [string]$WorkPath = 'build', [string]$PythonPath = '')
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'tools/Build-Product.ps1') -Product 'Codexon' -DistPath $DistPath -WorkPath $WorkPath -PythonPath $PythonPath
