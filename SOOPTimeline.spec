# -*- mode: python ; coding: utf-8 -*-

import json
import os
from pathlib import Path
import tempfile

import qtwebview2

from PyInstaller.utils.hooks import collect_all, collect_submodules


datas = []
binaries = []
hiddenimports = []

update_manifest_url = os.environ.get(
    "SOOP_TIMELINE_UPDATE_MANIFEST_URL",
    "",
).strip()
if update_manifest_url:
    update_channel_dir = Path(tempfile.mkdtemp(prefix="soop-timeline-build-"))
    update_channel_path = update_channel_dir / "update-channel.json"
    update_channel_path.write_text(
        json.dumps(
            {"manifest_url": update_manifest_url},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    datas.append((str(update_channel_path), "."))

for package in (
    "av",
    "ctranslate2",
    "faster_whisper",
    "google.genai",
    "nvidia.cublas",
    "nvidia.cudnn",
    "onnxruntime",
    "qtwebview2",
    "tokenizers",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

hiddenimports += collect_submodules("keyring.backends")

# qtwebview2 loads its .NET assemblies via clr.AddReference(get_absolute_path(
# 'lib/Microsoft.Web.WebView2.*')). In a frozen build get_absolute_path resolves
# against sys._MEIPASS, so the assemblies (and the native WebView2Loader under
# lib/runtimes/**) must sit at the BUNDLE ROOT's ./lib. collect_all() only places
# them under ./qtwebview2/lib, so WebView2 fails to initialise and the review
# player stays blank. Mirror the package's lib/ tree to ./lib to match dev layout.
_qtwebview2_lib = Path(qtwebview2.__file__).resolve().parent / "lib"
_bundled_webview2_assets = 0
if _qtwebview2_lib.is_dir():
    for _asset in _qtwebview2_lib.rglob("*"):
        if _asset.is_file():
            _rel_parent = _asset.relative_to(_qtwebview2_lib).parent
            _dest = "lib" if str(_rel_parent) == "." else str(Path("lib") / _rel_parent)
            datas.append((str(_asset), _dest))
            _bundled_webview2_assets += 1
print(
    f"[SOOPTimeline.spec] bundled {_bundled_webview2_assets} WebView2 assemblies "
    "into ./lib"
)
if _bundled_webview2_assets == 0:
    raise SystemExit(
        "SOOPTimeline.spec: no WebView2 assemblies found under qtwebview2/lib; "
        "the review player would ship broken."
    )

# faster-whisper's ctranslate2 backend only loads cuBLAS (plus the small cuDNN
# dispatcher, cudnn64_9.dll) for Whisper GPU inference; the heavy cuDNN engine
# DLLs below are never loaded. PyInstaller's bundled hooks (hook-nvidia.*)
# collect every CUDA DLL regardless of the collect_all() above, so these are
# dropped from the final Analysis TOC to cut ~760 MB without losing GPU speed.
# Verified against the loaded modules of a real large-v3-turbo CUDA
# transcription: only cublas64_12.dll, cublasLt64_12.dll and cudnn64_9.dll load.
UNUSED_CUDA_DLLS = {
    "cudnn_adv64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_graph64_9.dll",
    "cudnn_ops64_9.dll",
    "cudnn_heuristic64_9.dll",
    "cudnn_engines_precompiled64_9.dll",
    "cudnn_engines_runtime_compiled64_9.dll",
    "nvblas64_12.dll",
}

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)


def _strip_unused_cuda(toc):
    return [entry for entry in toc if os.path.basename(entry[0]).lower() not in UNUSED_CUDA_DLLS]


a.binaries = _strip_unused_cuda(a.binaries)
a.datas = _strip_unused_cuda(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SOOPTimeline",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
