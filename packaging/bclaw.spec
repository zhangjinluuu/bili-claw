# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 BiliClaw 打成**单文件、零依赖**的可执行产物。

    .venv/bin/python -m PyInstaller --clean --noconfirm packaging/bclaw.spec

产物在 ``dist/bclaw``（Windows 是 ``dist\\bclaw.exe``）。同一个 spec 三平台通用——
PyInstaller **不能交叉编译**，所以每个平台各跑一次，见 ``docs/落地计划.md`` §6 的矩阵。

## 这里为什么这么写

**``excludes`` 是这个文件里最值钱的部分。** PyInstaller 会顺着 import 关系把所有
够得着的东西都拖进来，包括只在开发时才用的东西；而我们要的是「下载即用」，体积
直接决定下载体验。列表里每一项都注明了排除理由，新增时请照写。

**不打包 ffmpeg。** MP4 合并已是纯 Python（``biliclaw.mp4``），ffmpeg 只服务
``--container mkv`` 这一条可选路径；把它塞进来要给每个平台加 40~80 MB，还要背上
LGPL/GPL 的分发义务。产物里没有 ffmpeg，``--check`` 也会如实报告这一点。
"""

from __future__ import annotations

import sys
from pathlib import Path

# spec 文件是被 exec 执行的，没有 __file__ 的常规语义，得自己找根目录
ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
sys.path.insert(0, str(ROOT / "src"))

from biliclaw import __version__  # noqa: E402 - 必须先补好 sys.path

#: 只在开发/测试期出现，运行时一行也用不到的包。
EXCLUDES = [
    # 图形界面：命令行工具不需要，tkinter 还带着一整套 Tcl/Tk 运行库
    "tkinter",
    "PIL",
    "matplotlib",
    "pandas",
    "numpy",
    "scipy",
    # 测试与打包工具自身
    "pytest",
    "_pytest",
    "unittest",
    "doctest",
    "pydoc",
    "setuptools",
    "pkg_resources",
    "pip",
    "wheel",
    "PyInstaller",
    # rich 只在 rich.syntax 里用到 pygments，而我们的输出没有代码高亮；
    # 它是整棵依赖树里除标准库外最大的一块（约 5 MB），值得单独排掉
    "pygments",
    # 平台分支：macOS 产物里不该出现 Windows/Linux 的浏览器支持代码
    "win32ctypes",
    "readline",
    "termios",
]

a = Analysis(  # noqa: F821 - Analysis/PYZ/EXE 都由 PyInstaller 注入
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    # 没有任何运行时数据文件：``--check`` 用的自检样本是 base64 常量，编在代码里。
    # 这正是不用 ``--add-data`` 的原因——多一个数据文件就多一处打包后会找不到的路径。
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="bclaw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 在 macOS 上基本用不了，而且压缩过的启动器更容易被杀软误报
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

print(f"BiliClaw {__version__} → {exe.name}")
