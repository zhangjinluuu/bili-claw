#!/bin/sh
#
# BiliClaw 一键安装（macOS / Linux）。
#
#   curl -fsSL https://github.com/zhangjinluuu/bili-claw/releases/download/v0.1.3/install.sh | sh
#
# **URL 里为什么写死 tag 而不是 /releases/latest？** 因为 latest 那条重定向会跳过
# 预发布，而验证期发的正是预发布——写 latest 的话，用户敲下第一条命令就吃 404，
# 连脚本都下载不到，脚本内部那套「先问 API 拿 tag」的兜底根本没机会运行。
# 版本转正之后，把 URL 换成 /releases/latest/download/install.sh 即可。
#
# 可选环境变量：
#   BCLAW_VERSION=0.1.3          装指定版本（默认装最新，含预发布；带不带 v 都行）
#   BCLAW_INSTALL_DIR=~/.local/bin   安装到别处
#   BCLAW_MIRROR=https://gh-proxy.com   国内镜像：在产物 URL 前拼上它
#       GitHub Releases 在国内常常只有几十 KB/s，20 MB 的包要下十几分钟。
#       镜像只代理 github.com 的下载路径，**不代理 api.github.com 的版本查询**；
#       所以网络差到连 API 都拿不到 tag 时，还得同时给 BCLAW_VERSION。
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
#: 国内镜像前缀，形如 ``https://gh-proxy.com``。空表示直连。
MIRROR="${BCLAW_MIRROR:-}"

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
    # 这里必须设超时：拿不到 tag 只是退化成 latest 重定向，一条兜底的信息不值得
    # 让用户对着空屏幕等下去（curl 默认用操作系统的 TCP 超时，连接悬空时是几十秒）。
    VERSION="$(curl -fsSL --connect-timeout 8 --max-time 20 \
        "https://api.github.com/repos/${REPO}/releases" 2>/dev/null \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1 || true)"
fi

# tag 带 `v` 前缀，人未必记得带。`BCLAW_VERSION=0.1.3` 直接拼进 URL 会变成
# /releases/download/0.1.3/ —— 一个 404，而错误信息看起来像是"这个版本没发布"。
case "$VERSION" in
    "" | v*) ;;
    [0-9]*) VERSION="v${VERSION}" ;;
esac

if [ -n "$VERSION" ]; then
    URL="https://github.com/${REPO}/releases/download/${VERSION}/${ARCHIVE}"
else
    URL="https://github.com/${REPO}/releases/latest/download/${ARCHIVE}"
fi

# 镜像就是在完整 URL 前面拼一个前缀（gh-proxy 系的约定：`<镜像>/<原始 URL>`）。
# 末尾斜杠可有可无，这里统一去掉，免得拼出双斜杠再被镜像判成另一个路径。
if [ -n "$MIRROR" ]; then
    URL="${MIRROR%/}/${URL}"
fi

printf '▸ 平台：%s-%s\n▸ 版本：%s\n' "$OS_TAG" "$ARCH_TAG" "${VERSION:-latest}"
if [ -n "$MIRROR" ]; then
    printf '▸ 镜像：%s\n' "$MIRROR"
elif [ -z "$VERSION" ]; then
    printf '  [提示] 没拿到版本号，走的是 latest 重定向；预发布版本会 404。\n' >&2
    printf '  [提示] 国内网络：在脚本 URL 前加镜像前缀，并把版本号显式给上\n' >&2
    printf '         （镜像只代理下载路径，取不到版本号）。具体命令见 README。\n' >&2
fi

# --- 下载并解压 -------------------------------------------------------------
# 解到临时目录再搬过去：中途失败时不会在安装目录里留下半个二进制，
# 而「半个二进制」对用户来说就是「命令有时候能跑有时候不能」。
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

printf '▸ 下载 %s\n' "$URL"
# 这里每个参数都不是装饰：
#   --connect-timeout 10  没有它时 curl 用操作系统的 TCP 超时（macOS 上约 75 秒）。
#                         GitHub 的部分边缘节点在有些网络里是整段不通的（DNS 正常、
#                         TCP 直接黑洞），症状就是「敲完命令后终端一片空白」。
#   --retry 3             链路抖动很常见，重试三次比让用户自己重跑整个脚本便宜。
#   --progress-bar        静默模式下 18MB 的下载和「卡死」在用户眼里没有区别。
#
# 外面再套一层断点续传：镜像和运营商常在长连接上降速甚至断流，20 MB 从断点续上比
# 重新下一遍便宜得多。只有文件已经有内容时才加 `-C -`——空文件上用它，行为取决于
# curl 版本，不值得赌。
attempt=0
while :; do
    if [ -s "$TMP/$ARCHIVE" ]; then
        curl -fL -C - --connect-timeout 10 --retry 2 --retry-delay 2 --progress-bar \
            "$URL" -o "$TMP/$ARCHIVE" && break
    else
        curl -fL --connect-timeout 10 --retry 2 --retry-delay 2 --progress-bar \
            "$URL" -o "$TMP/$ARCHIVE" && break
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 5 ]; then
        printf '\n' >&2
        die "下载失败：${URL}（已重试 5 次）
    1) 连不上 github.com —— 换网络，或先设代理再重跑：
         export https_proxy=http://127.0.0.1:7890
    2) 国内网络太慢 —— 改用镜像重跑（推荐，本机实测快 5 倍以上）：
         把本脚本的 URL 前面加上镜像前缀（gh-proxy 系的约定：<镜像>/<原始 URL>），
         并同时给出 BCLAW_VERSION 与 BCLAW_MIRROR 两个环境变量，
         因为镜像只代理下载路径，取不到版本号。完整命令见 README「国内加速」一节。
    3) 这个版本没挂 $ARCHIVE —— 用 BCLAW_VERSION=<已发布的版本> 指定
    4) 想手动装：用镜像前缀打开上面的 URL，解开后把 bclaw 放进 $INSTALL_DIR"
    fi
    printf '  %s\n' "[重试 $attempt/5] 已收 $(wc -c <"$TMP/$ARCHIVE" 2>/dev/null || echo 0) 字节，接着下…" >&2
    sleep 2
done

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
printf '  bclaw --logout        # 想退出登录：只删掉本机的登录态，程序留着\n'
printf '  bclaw --uninstall     # 以后不想用了：连程序带 Cookie 一起删掉\n'
