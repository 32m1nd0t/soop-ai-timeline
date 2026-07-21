$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "가상 환경을 찾을 수 없습니다: $python"
}

Push-Location -LiteralPath $projectRoot
try {
    & $python -m pip install -e ".[build,gpu-windows]"
    & $python -m PyInstaller --noconfirm --clean "SOOPTimeline.spec"
}
finally {
    Pop-Location
}

$exe = Join-Path $projectRoot "dist\SOOPTimeline.exe"
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

$version = (& $python -c "from soop_timeline import __version__; print(__version__)" | Select-Object -Last 1).Trim()
$downloadUrl = [string]$env:SOOP_TIMELINE_DOWNLOAD_URL
$releaseNotes = [string]$env:SOOP_TIMELINE_RELEASE_NOTES
$manifest = [ordered]@{
    version = $version
    download_url = $downloadUrl
    release_notes = $releaseNotes
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
}
$manifestPath = Join-Path $projectRoot "dist\update.json"
$manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Output $exe
Write-Output $manifestPath
