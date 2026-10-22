<#
.SYNOPSIS
    BiliClaw 一键安装（Windows）。

.EXAMPLE
    irm https://github.com/zhangjinluuu/bili-claw/releases/download/v0.1.3/install.ps1 | iex

    URL 里写死 tag 而不是用 /releases/latest：latest 那条重定向会跳过预发布，
    而验证期发的正是预发布，写 latest 会让用户的第一条命令直接 404，连脚本都拿不到。
    版本转正后换成 /releases/latest/download/install.ps1 即可。

    因为整条命令是一行，参数只能靠环境变量传：
        $env:BCLAW_VERSION = "0.1.3"          # 装指定版本（默认装最新，含预发布）
        $env:BCLAW_INSTALL_DIR = "$HOME\bin"  # 装到别处
        $env:BCLAW_MIRROR = "https://gh-proxy.com"  # 国内镜像：加速产物下载

.NOTES
    用 `irm | iex` 而不是「下载 zip 再解压」是有意的：浏览器或资源管理器下载的
    文件会被打上 Mark-of-the-Web，之后每次运行都要过 SmartScreen 的「Windows 已
    保护你的电脑」。PowerShell 直接下载的文件同样可能带这个标记，所以脚本最后会
    显式 Unblock-File 一次——这一步是免费的，而正经的解决办法是买代码签名证书
    （每年几百美元）。
#>

$ErrorActionPreference = 'Stop'

$Repo       = 'zhangjinluuu/bili-claw'
$InstallDir = if ($env:BCLAW_INSTALL_DIR) { $env:BCLAW_INSTALL_DIR } else { "$HOME\bin" }
$Version    = $env:BCLAW_VERSION
# 国内镜像前缀，形如 https://gh-proxy.com。空表示直连。
$Mirror     = $env:BCLAW_MIRROR

function Fail($Message) {
    Write-Host "✗ $Message" -ForegroundColor Red
    exit 1
}

# --- 认出平台 ---------------------------------------------------------------
# Windows arm64 刻意不做（见 docs/落地计划.md §6），所以这里直接拒绝，
# 而不是让用户下载一个装了跑不起来的包。
$Arch = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'x64' }
    'ARM64' { Fail '暂不支持 Windows arm64；请在 x64 上使用，或用 WSL 装 Linux 版。' }
    default { Fail "不认识的架构：$($env:PROCESSOR_ARCHITECTURE)" }
}

$Archive = "bclaw-windows-$Arch.zip"

# --- 挑版本 -----------------------------------------------------------------
# 先问 API 拿 tag：Release 标成预发布后 /releases/latest 会 404，而预发布正是
# 验证期的常态。API 失败（限流、离线）再退回 latest 重定向。
if (-not $Version) {
    try {
        $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases" `
            -Headers @{ 'User-Agent' = 'bili-claw-installer' }
        $Version = $releases | Select-Object -First 1 -ExpandProperty tag_name
    } catch {
        $Version = $null
    }
}

$Url = if ($Version) {
    "https://github.com/$Repo/releases/download/$Version/$Archive"
} else {
    "https://github.com/$Repo/releases/latest/download/$Archive"
}

# 镜像就是在完整 URL 前面拼一个前缀（gh-proxy 系的约定：<镜像>/<原始 URL>）。
if ($Mirror) {
    $Url = "$($Mirror.TrimEnd('/'))/$Url"
}

Write-Host "▸ 平台：windows-$Arch"
Write-Host "▸ 版本：$(if ($Version) { $Version } else { 'latest' })"

# --- 下载并解压 -------------------------------------------------------------
# 先解到临时目录再搬过去：中途失败不会在安装目录里留下半个 exe，
# 而半个 exe 对用户来说就是「命令有时候能跑有时候不能」。
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("bclaw-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Tmp -Force | Out-Null

try {
    $Zip = Join-Path $Tmp $Archive
    Write-Host "▸ 下载 $Url"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing
    } catch {
        Fail ("下载失败：$Url`n"
            + "  1) 网络不通：检查代理或换网络`n"
            + "  2) 国内网络太慢：设 `$env:BCLAW_MIRROR = 'https://gh-proxy.com' 后重跑`n"
            + "  3) 该版本没挂 $Archive：用 `$env:BCLAW_VERSION 指定一个已发布的版本")
    }

    Expand-Archive -Path $Zip -DestinationPath $Tmp -Force
    $Exe = Join-Path $Tmp 'bclaw.exe'
    if (-not (Test-Path $Exe)) { Fail '压缩包里没有 bclaw.exe' }

    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $Target = Join-Path $InstallDir 'bclaw.exe'
    Move-Item -Path $Exe -Destination $Target -Force

    # Mark-of-the-Web 是「按文件」记的，搬过来之后要再清一次
    Unblock-File -Path $Target -ErrorAction SilentlyContinue
} finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}

Write-Host "✓ 已安装到 $Target" -ForegroundColor Green
& $Target --version

# --- PATH -------------------------------------------------------------------
# 只动用户级 PATH，不碰系统级——不需要管理员权限，也不会影响别的账户。
$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($UserPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable('Path', "$UserPath;$InstallDir", 'User')
    Write-Host ''
    Write-Host "已把 $InstallDir 加进用户 PATH——**新开一个终端**才会生效。" -ForegroundColor Yellow
    $env:Path = "$env:Path;$InstallDir"   # 让本次会话里的下一步也能直接用
}

Write-Host ''
Write-Host '下一步：'
Write-Host '  bclaw --check         # 确认环境与登录态'
Write-Host '  bclaw --login         # 从本机浏览器取一次 B站 Cookie（下载 720P 以上需要）'
Write-Host '  bclaw --logout        # 想退出登录：只删掉本机的登录态，程序留着'
Write-Host '  bclaw --uninstall     # 以后不想用了：连程序带 Cookie 一起删掉'
