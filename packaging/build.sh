#!/usr/bin/env bash
#
# 构建 BiliClaw 的单文件产物并打成发布归档。
#
#   packaging/build.sh                # 构建 + 冒烟 + 归档
#   packaging/build.sh --no-archive   # 只构建，不归档（本地调试用）
#
# 这个开关**刻意不叫 --check**：`bclaw --check` 是检查运行环境，两回事，同名会让人
# 以为这里在跑环境检查。（tests/test_packaging.py 会挡住重名。）
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
[ "${1:-}" = "--no-archive" ] && ARCHIVE=0

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

# --- 进度标记与失败上报 -----------------------------------------------------
# CI 的原始日志**要登录才能下载**，而 GitHub 的 annotation 是匿名可读的。所以这里
# 每进入一个阶段就发一条 ::notice::，失败时发 ::error::——"最后一条 notice 是哪一步"
# 本身就告诉了你死在哪，既不用读日志，也不用猜。
#
# 这些标记刻意全是 ASCII：Windows 控制台的默认代码页不是 UTF-8，中文标记会和冒烟
# 测试那件事一样把人带偏（见下面的注释）。
#
# 本地跑时 CI 未设置，两个函数都不输出任何东西——终端里保持干净。
step() {
    [ -n "${CI:-}" ] || return 0
    printf '::notice::bclaw-build[%s] %s\n' "$TAG" "$1"
}

# 用法：<命令> || fail "能在 annotation 里定位到问题的描述"
fail() {
    printf '✗ %s\n' "$1" >&2
    [ -n "${CI:-}" ] && printf '::error::bclaw-build[%s] %s\n' "$TAG" "$1"
    exit 1
}

# 把日志尾部压成**一条**能放进 annotation 的字符串。
#
# 两个要点：
#  1. 换行要写成 %0A、百分号要先转成 %25——这是 GitHub 工作流命令的转义约定。
#     `%` 必须先处理，否则后写的 `%0A` 会被自己再转一遍，变成 `%250A`。
#  2. 优先从严错那一行开始截，而不是死板地取最后 N 行。PyInstaller 失败时
#     traceback 前面还压着几十行 INFO（"checking PYZ" 之类），只取尾部恰好会把
#     真正的原因切掉——上一轮 Windows 失败什么都没查出来，就是这个原因。
annotation_tail() {
    local file="$1" count="${2:-25}" text start
    start="$(grep -n -i -E 'traceback|error|exception' "$file" 2>/dev/null | tail -n 1 | cut -d: -f1 || true)"
    if [ -n "$start" ] && [ "$start" -gt 8 ]; then
        text="$(sed -n "$((start - 8)),\$p" "$file" | head -n "$count")"
    else
        text="$(tail -n "$count" "$file" 2>/dev/null)"
    fi
    text="$(printf '%s' "$text" | tr -d '\r')"
    # 参数展开里不要加引号：`${text//'%'/'%25'}` 会把引号本身写进结果，
    # 于是 `%` 变成字面的 `'%25'`。`%` 在模式里不是元字符，不用引。
    text="${text//%/%25}"
    printf '%s' "${text//$'\n'/%0A}"
}

step "platform detected: uname -s='$(uname -s)' uname -m='$(uname -m)' → $TAG"

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
        uv venv --python 3.12 || fail "uv venv --python 3.12 失败（interpreter=${VENV_PY}）"
    else
        python3 -m venv .venv || fail "python3 -m venv .venv 失败（没有 uv，也没有可用的 python3）"
    fi
fi

if [ ! -x "$VENV_PY" ]; then
    fail "虚拟环境建好了却找不到解释器：${VENV_PY} 不存在或不可执行"
fi
step "venv ready: ${VENV_PY}（$( "$VENV_PY" -c 'import sys; print(sys.version.split()[0], sys.platform)' )）"

echo "▸ 安装构建依赖"
step "installing build deps (uv=$([ "$(command -v uv || true)" != "" ] && echo yes || echo no))"
if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VENV_PY" -e . pyinstaller || fail "uv pip install -e . pyinstaller 失败"
else
    "$VENV_PY" -m pip install --quiet -e . pyinstaller || fail "pip install -e . pyinstaller 失败"
fi

VERSION="$("$VENV_PY" -c 'import biliclaw; print(biliclaw.__version__)')" \
    || fail "装完之后 import biliclaw 仍然失败——editable 安装没接上 src/ 布局"
echo "▸ BiliClaw $VERSION → $TAG"
step "biliclaw $VERSION 可导入，开始 PyInstaller"

# --- 构建 -------------------------------------------------------------------
# --clean 是必须的：spec 里改了 EXCLUDES 却复用上一次的缓存时，被排掉的包会
# 悄悄留在产物里，体积变化就成了唯一线索。
#
# 输出重定向到文件再判读，而不是直接流给终端：CI 上失败时唯一读得到的通道是
# annotation，而 GitHub **每个 step 只保留约 10 条**。把 40 行日志一行一条发出去，
# 真正的报错会被挤掉——Windows 那次就是这样，annotation 里只剩一堆 INFO，看不到
# 任何 traceback。所以这里压成**一条**。
PKG_LOG="$ROOT/build/pyinstaller.log"
mkdir -p "$ROOT/build"
# `-u` 与 `PYTHONFAULTHANDLER` 不是可有可无的讲究，是那次 Windows 失败逼出来的：
# 日志停在 "checking EXE" 就断了，连一行 traceback 都没有。两种可能各要一个开关——
#   1. 输出还压在缓冲区里没落盘 → `-u` 关掉缓冲，最后一行一定留得住；
#   2. PyInstaller 在 Windows 专属的图标/清单/资源处理里**硬崩**（那段是 ctypes 直接
#      调 Windows API，访问违例不会产生 Python 异常，只有一个进程级退出码）→
#      `PYTHONFAULTHANDLER=1` 把致命信号变成一份可见的回溯。
if ! PYTHONFAULTHANDLER=1 "$VENV_PY" -u -m PyInstaller --clean --noconfirm packaging/bclaw.spec >"$PKG_LOG" 2>&1; then
    tail -n 60 "$PKG_LOG" >&2
    fail "PyInstaller 构建失败：$(annotation_tail "$PKG_LOG" 25)"
fi

if [ ! -f "$BIN" ]; then
    fail "PyInstaller 报成功，但产物 ${BIN} 不在——先看 spec 里的 name 与 dist 路径"
fi

SIZE="$(wc -c <"$BIN" | tr -d ' ')"
# 花括号不能省：紧跟其后的是全角括号，不加大括号时 bash 会把那几个字节
# 当成变量名的一部分，`set -u` 于是报 "unbound variable"。
echo "▸ 产物：${BIN}（$((SIZE / 1024 / 1024)) MB）"
step "artifact built: ${BIN} ${SIZE} bytes"

# --- 冒烟测试 ---------------------------------------------------------------
# --version 证明能启动；--selftest 会真的用内置样本合并一次，是打包后唯一能在
# **构建机上**发现「这个构建的合并坏了」的地方。
#
# 这里刻意用 --selftest 而不是 --check：--check 的输出里有中文（"自检通过"），
# 而 Windows 控制台的默认代码页不是 UTF-8，`grep` 那些字永远不匹配——Windows CI
# 上真的因此整个 job 失败过一次，失败点看起来还像"合并内核坏了"。--selftest 走
# 纯 ASCII 输出、不碰网络、退出码就是结论，三个平台判读方式完全一致。
echo "▸ 冒烟测试"
"$BIN" --version >/dev/null || fail "产物无法启动：${BIN} --version 非零退出"

if ! SELFTEST_OUT="$("$BIN" --selftest 2>&1)"; then
    echo "$SELFTEST_OUT" >&2
    fail "合并内核自检未通过：$(printf '%s' "$SELFTEST_OUT" | tr -d '\r' | head -n 3 | tr '\n' ' ')"
fi
# 变量放句尾：`$VAR` 后面紧跟全角括号时，bash 会把那几个字节当成变量名的一部分。
# 这个坑已经踩过两次（上次是 `$BIN（`），所以 tests/test_packaging.py 里有一条
# 扫描 packaging/*.sh 的用例专门守它——别再靠人记得。
echo "  ✓ 启动正常，合并内核自检通过 —— $SELFTEST_OUT"
step "smoke test passed: $SELFTEST_OUT"

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
" || fail "打包 zip 失败（${NAME}.zip）"
    ARCHIVE_PATH="release/${NAME}.zip"
else
    # 归档里只放二进制本身：解压出来就该是可直接执行的单文件，
    # 而不是「先读 README 再决定放哪」。
    tar czf "release/${NAME}.tar.gz" -C dist bclaw || fail "打包 tar.gz 失败（${NAME}.tar.gz）"
    ARCHIVE_PATH="release/${NAME}.tar.gz"
fi

echo "▸ 归档：$ARCHIVE_PATH"
ls -l release/
step "archived: ${ARCHIVE_PATH} $(wc -c <"$ARCHIVE_PATH" | tr -d ' ') bytes"
