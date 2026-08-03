$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$installedDir = Join-Path $projectRoot "dist\SOOPTimeline"
$installedExe = Join-Path $installedDir "SOOPTimeline.exe"

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
$previousBuildMode = [string]$env:SOOP_TIMELINE_BUILD_MODE
try {
    if (Test-Path -LiteralPath $installedDir) {
        Remove-Item -LiteralPath $installedDir -Recurse -Force
    }
    & $python -m pip install -e ".[build]"
    if ($LASTEXITCODE -ne 0) {
        throw "CPU 설치본 빌드 의존성 설치에 실패했습니다. (exit code: $LASTEXITCODE)"
    }
    $env:SOOP_TIMELINE_BUILD_MODE = "installed"
    & $python -m PyInstaller --noconfirm --clean "SOOPTimeline.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "CPU 설치본 PyInstaller 빌드에 실패했습니다. (exit code: $LASTEXITCODE)"
    }
}
finally {
    if ([string]::IsNullOrEmpty($previousBuildMode)) {
        Remove-Item Env:\SOOP_TIMELINE_BUILD_MODE -ErrorAction SilentlyContinue
    }
    else {
        $env:SOOP_TIMELINE_BUILD_MODE = $previousBuildMode
    }
    Pop-Location
}

if (-not (Test-Path -LiteralPath $installedExe)) {
    throw "CPU 설치본 결과를 찾을 수 없습니다: $installedExe"
}

$signThumbprint = [string]$env:SOOP_TIMELINE_SIGN_CERT_THUMBPRINT
if (-not [string]::IsNullOrWhiteSpace($signThumbprint)) {
    $signTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($null -eq $signTool) {
        throw "코드 서명 인증서가 설정되었지만 signtool.exe를 찾지 못했습니다."
    }
    & $signTool.Source sign /sha1 $signThumbprint.Trim() /fd SHA256 /tr "http://timestamp.digicert.com" /td SHA256 $installedExe
    if ($LASTEXITCODE -ne 0) {
        throw "CPU 설치본 코드 서명이 실패했습니다."
    }
    & $signTool.Source verify /pa $installedExe
    if ($LASTEXITCODE -ne 0) {
        throw "CPU 설치본 코드 서명 검증에 실패했습니다."
    }
}

Write-Output $installedDir
Write-Output $installedExe
