$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "가상 환경을 찾을 수 없습니다: $python"
}

Push-Location -LiteralPath $projectRoot
try {
    & $python -m pip install -e ".[build]"
    & $python -m PyInstaller --noconfirm --clean "SOOPTimeline.spec"
}
finally {
    Pop-Location
}

$exe = Join-Path $projectRoot "dist\SOOPTimeline.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "EXE 빌드 결과를 찾을 수 없습니다: $exe"
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
