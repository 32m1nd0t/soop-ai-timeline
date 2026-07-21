# -*- mode: python ; coding: utf-8 -*-

import json
import os
from pathlib import Path
import tempfile

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
