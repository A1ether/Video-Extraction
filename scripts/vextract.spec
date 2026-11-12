# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 独立包配置（macOS Apple Silicon）。

由 scripts/build_standalone_app.py 调用；不要直接手改产物，改这里的收集规则。
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))

datas = [(os.path.join(ROOT, "src", "vextract", "web"), "vextract/web")]
binaries = []
hiddenimports = [
    # uvicorn 的动态导入不写进来，冻结后会 ImportError
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "websockets",
    "httptools",
    "uvloop",
]

# 本地转录：只带跨平台的 faster-whisper（不装 mlx / torch）。
for package in ("faster_whisper", "ctranslate2", "av", "onnxruntime", "tokenizers", "huggingface_hub"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# provider 是 importlib 动态发现的，静态分析看不到。
hiddenimports += collect_submodules("vextract.providers")
hiddenimports += collect_submodules("uvicorn")

a = Analysis(
    [os.path.join(ROOT, "scripts", "standalone_entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["mlx_whisper", "mlx", "torch", "whisper"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vextract",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

# 只产出 onedir 的 server 载荷；外层 .app 由 build_standalone_app.py 用
# Swift/WKWebView 原生外壳创建，避免"app 跳浏览器"。
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="server",
)
