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
english.NativeFailure=Installation did not complete. Check the installation log for details.
korean.NativeFailure=설치가 완료되지 않았습니다. 설치 로그에서 원인을 확인해 주세요.

[Files]
Source: "native-setup.ps1"; Flags: dontcopy
Source: "{#ProductDir}\*"; DestDir: "{code:ProductPath}"; Excludes: "CodexonRecovery.exe"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProductDir}\CodexonRecovery.exe"; DestDir: "{code:RecoveryDir}"; Flags: ignoreversion

[Registry]
Root: HKCU; Subkey: "Software\{#RegistryName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\{#ProtocolName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#RegistryName}.Recovery"; Flags: uninsdeletekey

[Code]
var InstallId: String; ActivationFailed: Boolean;

procedure ExitBootstrap(Code: Integer);
external 'ExitProcess@kernel32.dll stdcall';

function NativeSetup: Boolean;
var Request, Params, Arg, Lower, Script, Source: String; Proof: AnsiString;
    Lines: TArrayOfString;
    I, Count, Code: Integer; Service, Task: Variant;
begin
  Source := ExpandConstant('{srcexe}');
  Request := ExpandConstant('{param:CODEXONREQUEST|}');
  if Request <> '' then begin
    if not LoadStringFromFile(Request+'.ready', Proof) or
       (CompareText(Trim(String(Proof)), GetSHA256OfFile(Source)) <> 0) then
      RaiseException('Native installation request is invalid.');
    Service := CreateOleObject('Schedule.Service');
    Service.Connect();
    Task := Service.GetFolder('\').GetTask('Codexon-Setup-'+ExtractFileName(ExtractFileDir(Request)));
    if Task.State <> 4 then RaiseException('Native installation task is not running.');
    if CheckForMutexes('{#RegistryName}.Setup') then RaiseException('Another installation is running.');
    CreateMutex('{#RegistryName}.Setup');
    Result := True;
    Exit;
  end;
  ExtractTemporaryFile('native-setup.ps1');
  Script := ExpandConstant('{tmp}\native-setup.ps1');
  Request := ExpandConstant('{tmp}\native-request.ini');
  SetArrayLength(Lines,3);
  Lines[0] := 'source='+Source;
  Lines[1] := 'sha256='+GetSHA256OfFile(Source);
  Lines[2] := 'registry={#RegistryName}';
  Count := 0;
  for I := 1 to ParamCount do begin
    Arg := ParamStr(I); Lower := Lowercase(Arg);
    { Never forward the loader's /SL5 or other internal parameters. }
    if (Lower='/silent') or (Lower='/verysilent') or (Lower='/suppressmsgboxes') or
       (Lower='/norestart') or (Lower='/sp-') or (Lower='/log') or
       (Pos('/log=',Lower)=1) or (Pos('/dir=',Lower)=1) or (Pos('/lang=',Lower)=1) then begin
      SetArrayLength(Lines,Count+4);
      Lines[Count+3] := 'arg'+IntToStr(Count)+'='+Arg;
      Count := Count+1;
    end;
  end;
  SetArrayLength(Lines,Count+4);
  Lines[Count+3] := 'count='+IntToStr(Count);
  if not SaveStringsToUTF8File(Request,Lines,False) then RaiseException('Cannot write the installation request');
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File '+AddQuotes(Script)+' -Request '+AddQuotes(Request);
  if not ExecAndLogOutput(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
              '', SW_HIDE, ewWaitUntilTerminated, Code, nil) then Code := 1001;
  DeleteFile(Script);
  DeleteFile(Request);
  RemoveDir(ExpandConstant('{tmp}'));
  if (Code <> 0) and (Code <> 2) then
    SuppressibleMsgBox(CustomMessage('NativeFailure'), mbError, MB_OK, IDOK);
  { Returning False would report cancellation even after a successful worker. }
  ExitBootstrap(Code);
  Result := False;
end;

function InitializeSetup(): Boolean;
begin
  Result := NativeSetup();
  InstallId := GetDateTimeString('yyyymmdd-hhnnss', '-', ':') + '-' + IntToStr(Random(1000000));
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
    Args := '--native-install --install-root "' + ExpandConstant('{app}') + '" --product-dir "' + ProductPath('') +
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
