#ifndef ProductDir
  #error ProductDir must point to the verified frozen Codexon directory
#endif
#ifndef ProductVersion
  #error ProductVersion is required
#endif
#ifndef OutputDir
  #define OutputDir "..\dist-installer"
#endif
#ifdef Isolated
  #define ProductName "Codexon QA"
  #define RegistryName "Codexon-QA"
  #define ProtocolName "codexon-recovery-qa"
  #define ExtraArgs " --isolated-install --no-launch"
#else
  #define ProductName "Codexon"
  #define RegistryName "Codexon"
  #define ProtocolName "codexon-recovery"
  #define ExtraArgs ""
#endif

[Setup]
AppId={#RegistryName}
AppName={#ProductName}
AppVersion={#ProductVersion}
VersionInfoProductName={#ProductName}
AppPublisher=jisoq
AppPublisherURL=https://github.com/jisoq/Codexon
DefaultDirName={localappdata}\Programs\{#RegistryName}
DefaultGroupName={#ProductName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=no
SetupMutex={#RegistryName}.Setup
RestartApplications=no
DisableProgramGroupPage=yes
DisableDirPage=yes
UsePreviousAppDir=yes
UninstallDisplayIcon={code:ProductPath}\Codexon.exe
OutputDir={#OutputDir}
OutputBaseFilename=Codexon-Setup
SetupIconFile=..\icons\Codexon.ico
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[CustomMessages]
english.ActivationFailure=Codexon could not be activated. Existing launch paths and records are preserved. See install-result.json in the installation folder.
korean.ActivationFailure=Codexon 설치를 완료하지 못했습니다. 기존 실행 경로와 기록은 보존됩니다. 설치 폴더의 install-result.json을 확인하세요.
english.ActivationFailureTitle=Installation not completed
korean.ActivationFailureTitle=설치를 완료하지 못했습니다

[Files]
Source: "{#ProductDir}\*"; DestDir: "{code:ProductPath}"; Excludes: "CodexonRecovery.exe"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProductDir}\CodexonRecovery.exe"; DestDir: "{code:RecoveryDir}"; Flags: ignoreversion

[Registry]
Root: HKCU; Subkey: "Software\{#RegistryName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\{#ProtocolName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#RegistryName}.Recovery"; Flags: uninsdeletekey

[Code]
var InstallId: String; ActivationFailed: Boolean;

function InitializeSetup(): Boolean;
begin
  InstallId := GetDateTimeString('yyyymmdd-hhnnss', '-', ':') + '-' + IntToStr(Random(1000000));
  Result := True;
end;

function ProductPath(Param: String): String;
begin
  Result := ExpandConstant('{app}\versions\{#ProductVersion}-') + InstallId + '\Codexon';
end;

function RecoveryDir(Param: String): String;
begin
  Result := ExpandConstant('{app}\maintenance\{#ProductVersion}-') + InstallId;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var ExitCode: Integer; Args: String;
begin
  if CurStep = ssPostInstall then begin
    Args := '--install-root "' + ExpandConstant('{app}') + '" --product-dir "' + ProductPath('') +
      '" --report "' + ExpandConstant('{app}\install-result.json') + '"{#ExtraArgs}';
    if ActiveLanguage = 'english' then Args := Args + ' --language en'
    else Args := Args + ' --language ko';
    if not Exec(RecoveryDir('') + '\CodexonRecovery.exe', Args, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then begin
      ActivationFailed := True;
      Log(CustomMessage('ActivationFailure'));
      SuppressibleMsgBox(CustomMessage('ActivationFailure'), mbError, MB_OK, IDOK);
    end;
  end;
end;

function GetCustomSetupExitCode(): Integer;
begin
  if ActivationFailed then Result := 1001 else Result := 0;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and ActivationFailed then begin
    WizardForm.FinishedHeadingLabel.Caption := CustomMessage('ActivationFailureTitle');
    WizardForm.FinishedLabel.Caption := CustomMessage('ActivationFailure');
  end;
end;

function FallbackRecovery(): String;
var FindRec: TFindRec; Candidate: String;
begin
  Result := '';
  if FindFirst(ExpandConstant('{app}\maintenance\*'), FindRec) then begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then begin
          Candidate := ExpandConstant('{app}\maintenance\') + FindRec.Name + '\CodexonRecovery.exe';
          if FileExists(Candidate) then begin Result := Candidate; Exit; end;
        end;
      until not FindNext(FindRec);
    finally FindClose(FindRec); end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Tool, Args: String; ExitCode: Integer;
begin
  if CurUninstallStep <> usUninstall then Exit;
  if not RegQueryStringValue(HKCU, 'Software\{#RegistryName}', 'RecoveryPath', Tool) or not FileExists(Tool) then
    Tool := FallbackRecovery();
  if Tool = '' then
    RaiseException('연결 복구 도구의 위치를 확인하지 못했습니다. 설치 프로그램을 다시 실행한 뒤 제거하세요.');
  Args := '--prepare-uninstall --install-root "' + ExpandConstant('{app}') +
    '" --report "' + ExpandConstant('{app}\uninstall-check.json') + '"{#ExtraArgs}';
  if not Exec(Tool, Args, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
    RaiseException('진행 중인 연결이나 Codexon 실행 때문에 제거를 보류했습니다. Codexon을 종료하고 연결이 끝난 뒤 다시 실행하세요. 기록은 보존됩니다.');
end;
