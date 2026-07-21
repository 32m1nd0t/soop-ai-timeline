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
    "onnxruntime",
    "qtwebview2",
    "tokenizers",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

hiddenimports += collect_submodules("keyring.backends")

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
