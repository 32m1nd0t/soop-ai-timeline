$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
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
    $exe = Join-Path $projectRoot "dist\SOOPTimeline.exe"
    $manifestPath = Join-Path $projectRoot "dist\update.json"
    if (Test-Path -LiteralPath $exe) {
        Remove-Item -LiteralPath $exe -Force
    }
    if (Test-Path -LiteralPath $manifestPath) {
        Remove-Item -LiteralPath $manifestPath -Force
    }

    & $python -m pip install -e ".[build,gpu-windows]"
    if ($LASTEXITCODE -ne 0) {
        throw "빌드 의존성 설치에 실패했습니다. (exit code: $LASTEXITCODE)"
    }
    & $python -m PyInstaller --noconfirm --clean "SOOPTimeline.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 빌드에 실패했습니다. (exit code: $LASTEXITCODE)"
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $exe)) {
    throw "EXE 빌드 결과를 찾을 수 없습니다: $exe"
}

$signThumbprint = [string]$env:SOOP_TIMELINE_SIGN_CERT_THUMBPRINT
if (-not [string]::IsNullOrWhiteSpace($signThumbprint)) {
    $signTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($null -eq $signTool) {
        throw "SOOP_TIMELINE_SIGN_CERT_THUMBPRINT가 설정됐지만 signtool.exe를 찾지 못했습니다."
    }
    & $signTool.Source sign /sha1 $signThumbprint.Trim() /fd SHA256 /tr "http://timestamp.digicert.com" /td SHA256 $exe
    if ($LASTEXITCODE -ne 0) {
        throw "Windows 코드 서명에 실패했습니다."
    }
    & $signTool.Source verify /pa $exe
    if ($LASTEXITCODE -ne 0) {
        throw "Windows 코드 서명 검증에 실패했습니다."
    }
}

$versionOutput = & $python -c "from soop_timeline import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "앱 버전 조회에 실패했습니다. (exit code: $LASTEXITCODE)"
}
$version = ($versionOutput | Select-Object -Last 1).Trim()
if ([string]::IsNullOrWhiteSpace($version)) {
    throw "앱 버전 조회 결과가 비어 있습니다."
}
$downloadUrl = [string]$env:SOOP_TIMELINE_DOWNLOAD_URL
$releaseNotes = [string]$env:SOOP_TIMELINE_RELEASE_NOTES
$manifest = [ordered]@{
    version = $version
    download_url = $downloadUrl
    release_notes = $releaseNotes
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Output $exe
Write-Output $manifestPath
