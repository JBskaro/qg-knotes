; SPDX-License-Identifier: GPL-3.0-only
; QG Knotes installer for Inno Setup 6.6 or newer: per-user install, no administrator rights.
; build.py passes the version and the PyInstaller folder:
;   ISCC /DAppVersion=1.2.3 /DSourceDir=<folder with QG Knotes.exe> [/DIconFile=<setup icon>] /O<output folder>
; Setup parameter /LAUNCH=1 starts the program after a silent install (the program's own update uses it).

#ifndef AppVersion
  #error Pass the version: /DAppVersion=1.2.3
#endif
#ifndef SourceDir
  #error Pass the PyInstaller folder: /DSourceDir=<folder with QG Knotes.exe>
#endif

#define AppName "QG Knotes"
#define AppExe "QG Knotes.exe"
; The app sets the same id at start: taskbar pins and notifications belong to the shortcut
#define AppUserModelId "JBskaro.QGKnotes"

[Setup]
; Never change AppId: upgrades and the entry in Installed apps are matched by it
AppId={{2E6B7F9B-78D3-4A19-B42A-FC6B2EEFC593}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=JBskaro
VersionInfoVersion={#AppVersion}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern dynamic
ShowLanguageDialog=no
LanguageDetectionMethod=uilanguage
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
OutputBaseFilename=QG-Knotes-{#AppVersion}-setup
#ifdef IconFile
SetupIconFile={#IconFile}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Files left from an older PyInstaller build must not mix with the new ones
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall; Check: LaunchAfterInstall

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
const
  QuitWaitMs = 30000;
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

function FileInUse(const Path: String): Boolean;
var
  Stream: TFileStream;
begin
  Result := False;
  if not FileExists(Path) then
    Exit;
  try
    Stream := TFileStream.Create(Path, fmOpenReadWrite or fmShareExclusive);
    Stream.Free;
  except
    Result := True;
  end;
end;

{ Asks the running app to quit and waits until its exe is released. The app may ask the user to stop
  a recording first, so the user can retry or cancel. }
function CloseApp(const RunningMessage: String): Boolean;
var
  Exe: String;
  ResultCode, Waited: Integer;
begin
  Result := True;
  Exe := ExpandConstant('{app}\{#AppExe}');
  if not FileExists(Exe) then
    Exit;
  while True do
  begin
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Waited := 0;
    while FileInUse(Exe) and (Waited < QuitWaitMs) do
    begin
      Sleep(500);
      Waited := Waited + 500;
    end;
    if not FileInUse(Exe) then
      Exit;
    if SuppressibleMsgBox(FmtMessage(RunningMessage, ['{#AppName}']), mbError, MB_OKCANCEL, IDCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

{ The wizard offers a checkbox; a silent install starts the program only when asked to }
function LaunchAfterInstall(): Boolean;
begin
  Result := (not WizardSilent()) or (ExpandConstant('{param:launch|0}') = '1');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not CloseApp(SetupMessage(msgSetupAppRunningError)) then
    Result := SetupMessage(msgErrorCloseApplications);
end;

function InitializeUninstall(): Boolean;
begin
  Result := CloseApp(SetupMessage(msgUninstallAppRunningError));
end;

{ Settings, models and recordings stay; only the autostart entry pointing to the removed exe goes }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  if RegQueryStringValue(HKCU, RunKey, '{#AppName}', Command) then
    if Pos(Lowercase(ExpandConstant('{app}\')), Lowercase(Command)) > 0 then
      RegDeleteValue(HKCU, RunKey, '{#AppName}');
end;
