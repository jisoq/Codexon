$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (!(Test-Path -LiteralPath $python)) { $python = (Get-Command pythonw.exe -ErrorAction Stop).Source }
Start-Process -FilePath $python -ArgumentList ('"' + (Join-Path $PSScriptRoot 'run.py') + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
