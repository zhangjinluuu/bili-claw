#!/usr/bin/env bash
#
# 构建 BiliClaw 的单文件产物并打成发布归档。
#
#   packaging/build.sh          # 构建 + 冒烟 + 归档
#   packaging/build.sh --check  # 只构建，不归档（本地调试用）
#
# CI（.github/workflows/release.yml）与本地走的是**同一条路径**——只有本地跑得通，
# CI 才可能跑得通；两边各写一套的结果一定是「CI 绿了但发出来的东西不一样」。
#
# PyInstaller **不能交叉编译**：这个脚本在哪个平台上跑，就只产出那个平台的产物。
# 要五个平台的产物，就得在五个 runner 上各跑一次（见 docs/落地计划.md §6）。
#
# 依赖：bash、uname，以及 uv 或 python3 之一。Windows runner 上用 Git Bash。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ARCHIVE=1
[ "${1:-}" = "--check" ] && ARCHIVE=0

# --- 平台标签 ---------------------------------------------------------------
# 标签直接进产物文件名，所以宁可在这里失败，也不要产出一个名字含糊的包。
case "$(uname -s)" in
    Darwin)                OS=macos ;;
    Linux)                 OS=linux ;;
    MINGW*|MSYS*|CYGWIN*)  OS=windows ;;
    *) echo "✗ 不认识的系统：$(uname -s)" >&2; exit 1 ;;
esac

case "$(uname -m)" in
    arm64|aarch64) ARCH=arm64 ;;
    x86_64|amd64)  ARCH=x64 ;;
    *) echo "✗ 不认识的架构：$(uname -m)" >&2; exit 1 ;;
esac

# Windows arm64 刻意不做：没有可用的 CI runner，也没有真实需求（见计划 §6）。
if [ "$OS" = windows ] && [ "$ARCH" = arm64 ]; then
    echo "✗ Windows arm64 不在支持范围内" >&2
    exit 1
fi

TAG="${OS}-${ARCH}"

# --- 解释器与虚拟环境 -------------------------------------------------------
# 产物落在 .venv 之外的 dist/，但构建必须在有 pyinstaller 的环境里跑。
if [ "$OS" = windows ]; then
    VENV_PY=".venv/Scripts/python.exe"
    BIN="dist/bclaw.exe"
else
    VENV_PY=".venv/bin/python"
    BIN="dist/bclaw"
fi

if [ ! -x "$VENV_PY" ]; then
    echo "▸ 创建 .venv"
    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.12
    else
        python3 -m venv .venv
    fi
fi

echo "▸ 安装构建依赖"
if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VENV_PY" -e . pyinstaller
else
    "$VENV_PY" -m pip install --quiet -e . pyinstaller
fi

VERSION="$("$VENV_PY" -c 'import biliclaw; print(biliclaw.__version__)')"
echo "▸ BiliClaw $VERSION → $TAG"

# --- 构建 -------------------------------------------------------------------
# --clean 是必须的：spec 里改了 EXCLUDES 却复用上一次的缓存时，被排掉的包会
# 悄悄留在产物里，体积变化就成了唯一线索。
"$VENV_PY" -m PyInstaller --clean --noconfirm packaging/bclaw.spec

if [ ! -f "$BIN" ]; then
    echo "✗ 构建失败：找不到 $BIN" >&2
    exit 1
fi

SIZE="$(wc -c <"$BIN" | tr -d ' ')"
# 花括号不能省：紧跟其后的是全角括号，不加大括号时 bash 会把那几个字节
# 当成变量名的一部分，`set -u` 于是报 "unbound variable"。
echo "▸ 产物：${BIN}（$((SIZE / 1024 / 1024)) MB）"

# --- 冒烟测试 ---------------------------------------------------------------
# --version 证明能启动；--check 会真的用内置样本合并一次，是打包后唯一能在
# **构建机上**发现「这个构建的合并坏了」的地方。网络不通不影响自检那一行，
# 所以这里只要求自检通过，不对网络检查的结果提要求。
echo "▸ 冒烟测试"
"$BIN" --version >/dev/null

CHECK_OUT="$("$BIN" --check 2>&1 || true)"
if ! grep -q "自检通过" <<<"$CHECK_OUT"; then
    echo "✗ 合并内核自检未通过：" >&2
    echo "$CHECK_OUT" >&2
    exit 1
fi
echo "  ✓ 启动正常，合并内核自检通过"

[ "$ARCHIVE" -eq 0 ] && exit 0

# --- 归档 -------------------------------------------------------------------
# macOS / Linux 用 tar.gz，Windows 用 zip——三个平台各自的「解压不用额外装东西」
# 选择。scoop 与 Homebrew 都直接吃这两种格式。
mkdir -p release
NAME="bclaw-${TAG}"

if [ "$OS" = windows ]; then
    # 用 venv 里的 Python 打 zip，而不是 shell 的 zip 命令——Git Bash 不保证带 zip，
    # 而 Python 一定在（我们刚用它构建完）。
    "$VENV_PY" -c "
import zipfile
with zipfile.ZipFile(r'release/${NAME}.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    z.write(r'dist/bclaw.exe', 'bclaw.exe')
"
else
    # 归档里只放二进制本身：解压出来就该是可直接执行的单文件，
    # 而不是「先读 README 再决定放哪」。
    tar czf "release/${NAME}.tar.gz" -C dist bclaw
fi

echo "▸ 归档：release/${NAME}.$([ "$OS" = windows ] && echo zip || echo tar.gz)"
ls -l release/
