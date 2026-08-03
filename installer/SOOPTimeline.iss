#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{71BCA8C6-D7C4-4A45-A09A-04A81965D8EC}
AppName=SOOP AI 타임라인
AppVersion={#AppVersion}
AppPublisher=SOOP AI 타임라인 프로젝트
AppPublisherURL=https://github.com/32m1nd0t/soop-ai-timeline
AppSupportURL=https://github.com/32m1nd0t/soop-ai-timeline/issues
AppUpdatesURL=https://github.com/32m1nd0t/soop-ai-timeline/releases
DefaultDirName={localappdata}\Programs\SOOPTimeline
DefaultGroupName=SOOP AI 타임라인
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=SOOPTimeline-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
UninstallDisplayIcon={app}\SOOPTimeline.exe
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion={#AppVersion}.0

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 바로가기 만들기"; GroupDescription: "추가 바로가기:"; Flags: unchecked

[Files]
Source: "..\dist\SOOPTimeline\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\PRIVACY.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
Root: HKCU; Subkey: "Software\SOOPTimeline"; ValueType: string; ValueName: "InstallDir"; ValueData: "{app}"; Flags: uninsdeletekey

[UninstallDelete]
Type: filesandordirs; Name: "{app}\gpu-runtime"

[Icons]
Name: "{group}\SOOP AI 타임라인"; Filename: "{app}\SOOPTimeline.exe"
Name: "{autodesktop}\SOOP AI 타임라인"; Filename: "{app}\SOOPTimeline.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SOOPTimeline.exe"; Description: "SOOP AI 타임라인 실행"; Flags: nowait postinstall skipifsilent; Check: ShouldOfferLaunch
Filename: "{app}\SOOPTimeline.exe"; Flags: nowait; Check: ShouldRelaunch

[Code]
function HasExactParameter(const Value: String): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
  begin
    if CompareText(ParamStr(Index), Value) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function ShouldOfferLaunch(): Boolean;
begin
  Result := (not HasExactParameter('/NOLAUNCH=1')) and
            (not HasExactParameter('/RELAUNCH=1'));
end;

function ShouldRelaunch(): Boolean;
begin
  Result := (not HasExactParameter('/NOLAUNCH=1')) and
            HasExactParameter('/RELAUNCH=1');
end;
