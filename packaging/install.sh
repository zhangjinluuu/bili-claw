#!/bin/sh
#
# BiliClaw 一键安装（macOS / Linux）。
#
#   curl -fsSL https://github.com/zhangjinluuu/bili-claw/releases/download/v0.1.0/install.sh | sh
#
# **URL 里为什么写死 tag 而不是 /releases/latest？** 因为 latest 那条重定向会跳过
# 预发布，而验证期发的正是预发布——写 latest 的话，用户敲下第一条命令就吃 404，
# 连脚本都下载不到，脚本内部那套「先问 API 拿 tag」的兜底根本没机会运行。
# 版本转正之后，把 URL 换成 /releases/latest/download/install.sh 即可。
#
# 可选环境变量：
#   BCLAW_VERSION=0.1.0          装指定版本（默认装最新，含预发布）
#   BCLAW_INSTALL_DIR=~/.local/bin   安装到别处
#
# **为什么坚持用 curl 而不是让用户去浏览器下载？**
# 这不是风格偏好。浏览器下载的文件会被打上隔离标记（macOS 的 com.apple.quarantine），
# 双击/首次执行时直接报「已损坏，无法打开」，而消除它的正规办法是买一个
# Apple 开发者账号做签名与公证（$99/年）。命令行工具下载**不产生这个标记**，
# 于是整个问题被绕开了——这是本脚本存在的核心理由，不是顺手的方便。

set -eu

REPO="zhangjinluuu/bili-claw"
INSTALL_DIR="${BCLAW_INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${BCLAW_VERSION:-}"

die() {
    printf '✗ %s\n' "$1" >&2
    exit 1
}

# --- 认出平台 ---------------------------------------------------------------
# 产物名必须与 .github/workflows/release.yml 里的 matrix.tag 完全一致；
# 两边对不上时的表现是 404，而 404 在用户看来和「项目没发版」没有区别，
# 所以这里宁可啰嗦也要把名字拼齐。
case "$(uname -s)" in
    Darwin) OS_TAG=macos ;;
    Linux)  OS_TAG=linux ;;
    *)
        die "只支持 macOS 与 Linux（当前：$(uname -s)）。Windows 请用 install.ps1。"
        ;;
esac

case "$(uname -m)" in
    arm64|aarch64) ARCH_TAG=arm64 ;;
    x86_64|amd64)  ARCH_TAG=x64 ;;
    *) die "不支持的架构：$(uname -m)" ;;
esac

ARCHIVE="bclaw-${OS_TAG}-${ARCH_TAG}.tar.gz"

command -v curl >/dev/null 2>&1 || die "需要 curl"
command -v tar  >/dev/null 2>&1 || die "需要 tar"

# --- 挑版本 -----------------------------------------------------------------
# 先问 API 拿 tag，而不是直接用 /releases/latest：Release 一旦被标成预发布，
# latest 那条重定向就会 404，而预发布正是我们验证期的常态。API 拿不到时
# （限流、离线）再退回 latest 重定向，两条路都留。
if [ -z "$VERSION" ]; then
    VERSION="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases" 2>/dev/null \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1 || true)"
fi

if [ -n "$VERSION" ]; then
    URL="https://github.com/${REPO}/releases/download/${VERSION}/${ARCHIVE}"
else
    URL="https://github.com/${REPO}/releases/latest/download/${ARCHIVE}"
fi

printf '▸ 平台：%s-%s\n▸ 版本：%s\n' "$OS_TAG" "$ARCH_TAG" "${VERSION:-latest}"

# --- 下载并解压 -------------------------------------------------------------
# 解到临时目录再搬过去：中途失败时不会在安装目录里留下半个二进制，
# 而「半个二进制」对用户来说就是「命令有时候能跑有时候不能」。
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

printf '▸ 下载 %s\n' "$URL"
curl -fsSL "$URL" -o "$TMP/$ARCHIVE" \
    || die "下载失败。若这是首次发布，请确认 $ARCHIVE 已挂在该版本的 Release 上。"

tar xzf "$TMP/$ARCHIVE" -C "$TMP" || die "解压失败，下载到的可能不是一个 tar.gz"

mkdir -p "$INSTALL_DIR"
install -m 755 "$TMP/bclaw" "$INSTALL_DIR/bclaw" 2>/dev/null \
    || { cp "$TMP/bclaw" "$INSTALL_DIR/bclaw" && chmod 755 "$INSTALL_DIR/bclaw"; }

# bili-claw 是正式名，bclaw 是日常用的短名；源码安装两个都有，这里保持一致。
ln -sf bclaw "$INSTALL_DIR/bili-claw"

printf '✓ 已安装到 %s/bclaw\n' "$INSTALL_DIR"
"$INSTALL_DIR/bclaw" --version

# --- PATH 提示 --------------------------------------------------------------
# 只提示、不改用户的 shell 配置：往别人的 .zshrc 里追加内容是安装脚本最不该做的事。
case ":${PATH}:" in
    *":${INSTALL_DIR}:"*) ;;
    *)
        printf '\n把它加进 PATH：\n'
        printf '  echo '\''export PATH="%s:$PATH"'\'' >> ~/.zshenv\n' "$INSTALL_DIR"
        ;;
esac

printf '\n下一步：\n'
printf '  bclaw --check         # 确认环境与登录态\n'
printf '  bclaw --login         # 从本机浏览器取一次 B站 Cookie（下载 720P 以上需要）\n'
