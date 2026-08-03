#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{42E06858-A0D8-4981-8EA8-01E538B9B0C3}
AppName=SOOP AI 타임라인 NVIDIA GPU 구성요소
AppVersion={#AppVersion}
AppPublisher=SOOP AI 타임라인 프로젝트
AppPublisherURL=https://github.com/32m1nd0t/soop-ai-timeline
AppSupportURL=https://github.com/32m1nd0t/soop-ai-timeline/issues
DefaultDirName={reg:HKCU\Software\SOOPTimeline,InstallDir|{localappdata}\Programs\SOOPTimeline}
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=SOOPTimeline-GPU-Addon
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
Uninstallable=no
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion={#AppVersion}.0

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Files]
Source: "..\dist\gpu-addon\cublas64_12.dll"; DestDir: "{app}\gpu-runtime"; Flags: ignoreversion
Source: "..\dist\gpu-addon\cublasLt64_12.dll"; DestDir: "{app}\gpu-runtime"; Flags: ignoreversion
Source: "..\dist\gpu-addon\cudnn64_9.dll"; DestDir: "{app}\gpu-runtime"; Flags: ignoreversion
Source: "..\dist\gpu-addon\NVIDIA-cuBLAS-License.txt"; DestDir: "{app}\gpu-runtime\licenses"; Flags: ignoreversion
Source: "..\dist\gpu-addon\NVIDIA-cuDNN-License.txt"; DestDir: "{app}\gpu-runtime\licenses"; Flags: ignoreversion

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not FileExists(ExpandConstant('{app}\SOOPTimeline.exe')) then
    Result := 'SOOP AI 타임라인 기본 앱을 먼저 설치해 주세요.';
end;
