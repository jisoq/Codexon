param([Parameter(Mandatory)][string]$Installer,
      [Parameter(Mandatory)][string]$BrokenInstaller,
      [Parameter(Mandatory)][string]$Output,
      [Parameter(Mandatory)][string]$Python)
$ErrorActionPreference='Stop'
# This fixture installs a disposable signing certificate only on the CI machine.
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Use the existing Codex package for local MSIX checks; the synthetic fixture is CI-only.' }
$root=[IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $root) { throw 'A fresh QA output directory is required' }
New-Item -ItemType Directory -Path $root | Out-Null
$payload=Join-Path $root 'payload'
New-Item -ItemType Directory -Path $payload | Out-Null
$sdk=Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\makeappx.exe" | Sort-Object FullName -Descending | Select-Object -First 1
if (!$sdk) { throw 'Windows SDK MakeAppx is required' }
$signer=Join-Path $sdk.DirectoryName 'signtool.exe'
$id='Codexon.InstallQA.'+[guid]::NewGuid().ToString('N')
$publisher='CN='+$id
$certificate=$null
$package=$null
try {
    $source=@'
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
public class Launcher {
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern int GetCurrentPackageFullName(ref uint n,IntPtr p);
 public static int Main(string[] a) {
  uint n=0; bool packaged=GetCurrentPackageFullName(ref n,IntPtr.Zero)==122;
  int code=1001;
  if(packaged) { using(var p=Process.Start(new ProcessStartInfo(a[0],a[1]){UseShellExecute=false})) {p.WaitForExit();code=p.ExitCode;} }
  File.WriteAllText(a[2],"{\"packaged\":"+packaged.ToString().ToLowerInvariant()+",\"exit_code\":"+code+"}");
  return code;
 }
}
'@
    $cs=Join-Path $root 'Launcher.cs'; Set-Content -LiteralPath $cs -Value $source -Encoding utf8
    & "$env:SystemRoot\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe ('/out:'+(Join-Path $payload 'Launcher.exe')) $cs
    if ($LASTEXITCODE) { throw 'MSIX launcher compilation failed' }
    Add-Type -AssemblyName System.Drawing
    foreach($size in @(44,150)) {
        $bitmap=New-Object Drawing.Bitmap $size,$size
        try { $bitmap.Save((Join-Path $payload ('logo'+$size+'.png')),[Drawing.Imaging.ImageFormat]::Png) } finally { $bitmap.Dispose() }
    }
    $manifest=@"
<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10" xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10" xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities" IgnorableNamespaces="uap rescap">
 <Identity Name="$id" Publisher="$publisher" Version="1.0.0.0" ProcessorArchitecture="x64"/>
 <Properties><DisplayName>Codexon installation QA</DisplayName><PublisherDisplayName>Codexon QA</PublisherDisplayName><Logo>logo44.png</Logo></Properties>
 <Dependencies><TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.26100.0"/></Dependencies>
 <Resources><Resource Language="en-us"/></Resources>
 <Applications><Application Id="Launcher" Executable="Launcher.exe" EntryPoint="Windows.FullTrustApplication"><uap:VisualElements DisplayName="Codexon QA" Description="Disposable installation fixture" Square150x150Logo="logo150.png" Square44x44Logo="logo44.png" BackgroundColor="transparent"/></Application></Applications>
 <Capabilities><rescap:Capability Name="runFullTrust"/></Capabilities>
</Package>
"@
    Set-Content -LiteralPath (Join-Path $payload 'AppxManifest.xml') -Value $manifest -Encoding utf8
    $msix=Join-Path $root 'fixture.msix'
    & $sdk.FullName pack /d $payload /p $msix /o
    if ($LASTEXITCODE) { throw 'MSIX fixture packaging failed' }
    $certificate=New-SelfSignedCertificate -Type Custom -Subject $publisher -KeyUsage DigitalSignature -CertStoreLocation Cert:\CurrentUser\My -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3','2.5.29.19={text}')
    $certFile=Join-Path $root 'fixture.cer'
    Export-Certificate -Cert $certificate -FilePath $certFile | Out-Null
    Import-Certificate -FilePath $certFile -CertStoreLocation Cert:\LocalMachine\TrustedPeople | Out-Null
    & $signer sign /fd SHA256 /sha1 $certificate.Thumbprint $msix
    if ($LASTEXITCODE) { throw 'MSIX fixture signing failed' }
    Add-AppxPackage -Path $msix
    $package=Get-AppxPackage -Name $id
    if (!$package) { throw 'MSIX fixture registration failed' }
    $reports=Join-Path $root 'launches';New-Item -ItemType Directory -Path $reports | Out-Null
    $env:CODEXON_QA_PACKAGE_FAMILY=$package.PackageFamilyName
    $env:CODEXON_QA_PACKAGE_LAUNCHER=Join-Path $package.InstallLocation 'Launcher.exe'
    $env:CODEXON_QA_PACKAGE_REPORTS=$reports
    & $Python -B tools/run_ui_checks.py -- $Python -B tools/verify_installation.py --installer $Installer --broken-installer $BrokenInstaller --output (Join-Path $root 'installation')
    if ($LASTEXITCODE) { throw 'MSIX-origin installation checks failed' }
} finally {
    if ($package) { Remove-AppxPackage -Package $package.PackageFullName }
    if ($certificate) {
        Remove-Item -LiteralPath ('Cert:\LocalMachine\TrustedPeople\'+$certificate.Thumbprint) -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath ('Cert:\CurrentUser\My\'+$certificate.Thumbprint) -ErrorAction SilentlyContinue
    }
    Remove-Item Env:CODEXON_QA_PACKAGE_FAMILY,Env:CODEXON_QA_PACKAGE_LAUNCHER,Env:CODEXON_QA_PACKAGE_REPORTS -ErrorAction SilentlyContinue
}
