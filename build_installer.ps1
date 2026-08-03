param(
    [switch]$SkipExeBuild
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $projectRoot "dist\SOOPTimeline.exe"
$installedDir = Join-Path $projectRoot "dist\SOOPTimeline"
$installedExe = Join-Path $installedDir "SOOPTimeline.exe"
$setup = Join-Path $projectRoot "dist\SOOPTimeline-Setup.exe"
$gpuSetup = Join-Path $projectRoot "dist\SOOPTimeline-GPU-Addon.exe"
$gpuStaging = Join-Path $projectRoot "dist\gpu-addon"
$manifestPath = Join-Path $projectRoot "dist\update.json"
$issPath = Join-Path $projectRoot "installer\SOOPTimeline.iss"
$gpuIssPath = Join-Path $projectRoot "installer\SOOPTimeline-GPU-Addon.iss"
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
        & (Join-Path $projectRoot "build_installed_app.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "CPU 설치본 빌드가 실패했습니다. (exit code: $LASTEXITCODE)"
        }
    }
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "휴대용 EXE가 없습니다: $exe"
    }
    if (-not (Test-Path -LiteralPath $installedExe)) {
        throw "설치 프로그램에 넣을 CPU 앱이 없습니다: $installedExe"
    }
    if (Test-Path -LiteralPath $setup) {
        Remove-Item -LiteralPath $setup -Force
    }
    if (Test-Path -LiteralPath $gpuSetup) {
        Remove-Item -LiteralPath $gpuSetup -Force
    }
    if (Test-Path -LiteralPath $gpuStaging) {
        Remove-Item -LiteralPath $gpuStaging -Recurse -Force
    }
    New-Item -ItemType Directory -Path $gpuStaging | Out-Null

    $pythonRoot = Split-Path -Parent (Split-Path -Parent $python)
    $sitePackages = Join-Path $pythonRoot "Lib\site-packages"
    $cublasBin = Join-Path $sitePackages "nvidia\cublas\bin"
    $cudnnBin = Join-Path $sitePackages "nvidia\cudnn\bin"
    foreach ($runtimeFile in @(
        (Join-Path $cublasBin "cublas64_12.dll"),
        (Join-Path $cublasBin "cublasLt64_12.dll"),
        (Join-Path $cudnnBin "cudnn64_9.dll")
    )) {
        if (-not (Test-Path -LiteralPath $runtimeFile)) {
            throw "GPU 추가 구성요소 파일을 찾지 못했습니다: $runtimeFile"
        }
        Copy-Item -LiteralPath $runtimeFile -Destination $gpuStaging
    }

    if (-not (Test-Path -LiteralPath $sitePackages)) {
        throw "Python site-packages 경로를 찾지 못했습니다: $sitePackages"
    }
    $cublasInfo = Get-ChildItem -LiteralPath $sitePackages -Directory | Where-Object { $_.Name -like "nvidia_cublas_cu12-*.dist-info" } | Select-Object -First 1
    $cudnnInfo = Get-ChildItem -LiteralPath $sitePackages -Directory | Where-Object { $_.Name -like "nvidia_cudnn_cu12-*.dist-info" } | Select-Object -First 1
    if ($null -eq $cublasInfo -or $null -eq $cudnnInfo) {
        throw "NVIDIA GPU 런타임 라이선스 디렉터리를 찾지 못했습니다."
    }
    Copy-Item -LiteralPath (Join-Path $cublasInfo.FullName "License.txt") -Destination (Join-Path $gpuStaging "NVIDIA-cuBLAS-License.txt")
    Copy-Item -LiteralPath (Join-Path $cudnnInfo.FullName "License.txt") -Destination (Join-Path $gpuStaging "NVIDIA-cuDNN-License.txt")

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
    & $isccPath "/DAppVersion=$version" $gpuIssPath
    if ($LASTEXITCODE -ne 0) {
        throw "GPU 추가 구성요소 빌드가 실패했습니다. (exit code: $LASTEXITCODE)"
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $setup)) {
    throw "설치 프로그램 빌드 결과를 찾을 수 없습니다: $setup"
}
if (-not (Test-Path -LiteralPath $gpuSetup)) {
    throw "GPU 추가 구성요소 빌드 결과를 찾을 수 없습니다: $gpuSetup"
}

$signThumbprint = [string]$env:SOOP_TIMELINE_SIGN_CERT_THUMBPRINT
if (-not [string]::IsNullOrWhiteSpace($signThumbprint)) {
    $signTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($null -eq $signTool) {
        throw "코드 서명 인증서가 설정되었지만 signtool.exe를 찾지 못했습니다."
    }
    foreach ($signTarget in @($setup, $gpuSetup)) {
        & $signTool.Source sign /sha1 $signThumbprint.Trim() /fd SHA256 /tr "http://timestamp.digicert.com" /td SHA256 $signTarget
        if ($LASTEXITCODE -ne 0) {
            throw "설치 프로그램 코드 서명이 실패했습니다: $signTarget"
        }
        & $signTool.Source verify /pa $signTarget
        if ($LASTEXITCODE -ne 0) {
            throw "설치 프로그램 코드 서명 검증에 실패했습니다: $signTarget"
        }
    }
}

$installerUrl = [string]$env:SOOP_TIMELINE_INSTALLER_URL
$gpuAddonUrl = [string]$env:SOOP_TIMELINE_GPU_ADDON_URL
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
    gpu_addon_url = $gpuAddonUrl
    gpu_addon_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $gpuSetup).Hash.ToLowerInvariant()
    portable_url = $portableUrl
    portable_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
    release_notes = $releaseNotes
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Output $exe
Write-Output $setup
Write-Output $gpuSetup
Write-Output $manifestPath
