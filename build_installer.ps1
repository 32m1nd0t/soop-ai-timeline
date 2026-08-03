param(
    [switch]$SkipExeBuild
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $projectRoot "dist\SOOPTimeline.exe"
$setup = Join-Path $projectRoot "dist\SOOPTimeline-Setup.exe"
$manifestPath = Join-Path $projectRoot "dist\update.json"
$issPath = Join-Path $projectRoot "installer\SOOPTimeline.iss"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
}
else {
    $pythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Python 실행 파일을 찾을 수 없습니다."
    }
    $python = $pythonCommand.Source
}

Push-Location -LiteralPath $projectRoot
try {
    if (-not $SkipExeBuild) {
        & (Join-Path $projectRoot "build_exe.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "EXE 빌드가 실패했습니다. (exit code: $LASTEXITCODE)"
        }
    }
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "설치 프로그램에 넣을 EXE가 없습니다: $exe"
    }
    if (Test-Path -LiteralPath $setup) {
        Remove-Item -LiteralPath $setup -Force
    }

    $isccPath = ""
    if (-not [string]::IsNullOrWhiteSpace([string]$env:INNO_SETUP_COMPILER)) {
        $isccPath = [string]$env:INNO_SETUP_COMPILER
    }
    if ([string]::IsNullOrWhiteSpace($isccPath)) {
        $isccCommand = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
        if ($null -ne $isccCommand) {
            $isccPath = $isccCommand.Source
        }
    }
    if ([string]::IsNullOrWhiteSpace($isccPath) -and ${env:ProgramFiles(x86)}) {
        $candidate = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
        if (Test-Path -LiteralPath $candidate) {
            $isccPath = $candidate
        }
    }
    if ([string]::IsNullOrWhiteSpace($isccPath) -and $env:LOCALAPPDATA) {
        $candidate = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
        if (Test-Path -LiteralPath $candidate) {
            $isccPath = $candidate
        }
    }
    if ([string]::IsNullOrWhiteSpace($isccPath)) {
        throw "Inno Setup 6 컴파일러(ISCC.exe)를 찾지 못했습니다. INNO_SETUP_COMPILER 환경 변수로 경로를 지정할 수 있습니다."
    }

    $versionOutput = & $python -c "from soop_timeline import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0) {
        throw "앱 버전 조회가 실패했습니다. (exit code: $LASTEXITCODE)"
    }
    $version = ($versionOutput | Select-Object -Last 1).Trim()
    if ([string]::IsNullOrWhiteSpace($version)) {
        throw "앱 버전 조회 결과가 비어 있습니다."
    }

    & $isccPath "/DAppVersion=$version" $issPath
    if ($LASTEXITCODE -ne 0) {
        throw "설치 프로그램 빌드가 실패했습니다. (exit code: $LASTEXITCODE)"
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $setup)) {
    throw "설치 프로그램 빌드 결과를 찾을 수 없습니다: $setup"
}

$signThumbprint = [string]$env:SOOP_TIMELINE_SIGN_CERT_THUMBPRINT
if (-not [string]::IsNullOrWhiteSpace($signThumbprint)) {
    $signTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($null -eq $signTool) {
        throw "코드 서명 인증서가 설정되었지만 signtool.exe를 찾지 못했습니다."
    }
    & $signTool.Source sign /sha1 $signThumbprint.Trim() /fd SHA256 /tr "http://timestamp.digicert.com" /td SHA256 $setup
    if ($LASTEXITCODE -ne 0) {
        throw "설치 프로그램 코드 서명이 실패했습니다."
    }
    & $signTool.Source verify /pa $setup
    if ($LASTEXITCODE -ne 0) {
        throw "설치 프로그램 코드 서명 검증에 실패했습니다."
    }
}

$installerUrl = [string]$env:SOOP_TIMELINE_INSTALLER_URL
$portableUrl = [string]$env:SOOP_TIMELINE_PORTABLE_URL
if ([string]::IsNullOrWhiteSpace($portableUrl)) {
    $portableUrl = [string]$env:SOOP_TIMELINE_DOWNLOAD_URL
}
$releaseNotes = [string]$env:SOOP_TIMELINE_RELEASE_NOTES
$manifest = [ordered]@{
    version = $version
    download_url = $installerUrl
    installer_url = $installerUrl
    installer_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash.ToLowerInvariant()
    portable_url = $portableUrl
    portable_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
    release_notes = $releaseNotes
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Output $exe
Write-Output $setup
Write-Output $manifestPath
