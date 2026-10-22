# 🎰 哔哩娃娃机 / BiliClaw

> **投币，出爪，把热爱抓回家。**

把 B站视频下载到本地，支持清晰度选择、登录态解锁 1080P+、DASH 音视频并发下载与**内置的无损合并**。

命令行是 `bili-claw`（短写 `bclaw`）：

```console
$ bclaw BV17wpp6SEe9 -q 1080
  🎰 抓到了：改写战局！多库替补造2球+造红牌 哈兰德破门 十人利物浦0比3曼城.mp4　downloads/吉罗很快乐 · 524.4 MB
```

> 这个项目的价值在于把「清晰度选择」这件事做对——B站会静默降级你的请求，绝大多数工具只告诉你
> 它下到了什么，而不告诉你**为什么没拿到你要的那一档**。BiliClaw 的原则是：绝不静默降级。

---

## 特性

- **清晰的清晰度语义**：`-q 1080` 的含义是"不超过 1080P 的最高可用档位"，而不是"必须是 1080P"
- **降级必须告知**：请求 4K 但账号没有大会员时，会明确告诉你实际拿到了什么、为什么
- **严格模式**：`--on-unavailable fail` 让档位不匹配直接报错退出，适合脚本
- **登录态解锁**：支持 Cookie 文件，1080P / 1080P+ / 4K / 8K 按账号权限如实反映
- **一键取 Cookie**：`--login` 直接读本机浏览器的 Cookie 并当场验证（支持矩阵见下）
- **手机扫码也能登录**：`--login --qrcode` 在终端里画二维码，用 B站 App 扫——不碰浏览器、不读钥匙串
- **DASH 并发下载**：视频流多分片并发 + 断点续传，中断后重跑同一命令即可继续
- **CDN 链接过期自动换链**：B站下载链接约 2 小时失效，遇 403 自动重新请求 playurl
- **无损合并，且不依赖 ffmpeg**：合并由程序自己完成，只做 remux，不重新编码，画质音质零损失
- **机器可用的退出码**：0/1/2/3/4/5/6/7 区分成功、部分失败、参数错误、网络、权限、风控、缺依赖、磁盘

## 安装

**下载一个文件，直接跑。** 不需要 Python，不需要 ffmpeg，不需要任何运行时。

```bash
# macOS（Apple 芯片）与 Linux（x64）
curl -fsSL https://github.com/zhangjinluuu/bili-claw/releases/download/v0.1.2/install.sh | sh
```

Windows（PowerShell）：

```powershell
irm https://github.com/zhangjinluuu/bili-claw/releases/download/v0.1.2/install.ps1 | iex
```

验证环境：

```bash
bclaw --check
```

> **URL 里为什么写死了 `v0.1.2` 而不是用 `/releases/latest`？** 因为 `latest` 会跳过
> **预发布**，而目前发布的正是预发布（三平台的真实安装还没逐个验过）。写 `latest`
> 的话你敲下第一条命令就是 404，而且报错信息看起来跟「这个项目还没发版」一模一样。
> 版本转正后，把 URL 里的 `releases/download/v0.1.2` 换成 `releases/latest/download` 即可。
>
> 想装别的版本就改 URL 里的 tag，或者装完直接用 `BCLAW_VERSION=0.1.2` 环境变量指定。

> **为什么用 `curl` 而不是去 Release 页面点下载？** 这不是风格偏好。浏览器下载的文件会被打上
> 隔离标记（macOS 的 `com.apple.quarantine`、Windows 的 Mark-of-the-Web），于是 macOS 报
> 「已损坏，无法打开」、Windows 弹 SmartScreen 警告。用命令行工具下载**不产生这个标记**，
> 等于绕过了代码签名问题——而正式解决它需要一年 $99 的 Apple 开发者账号。

<details>
<summary>从源码安装（开发用）</summary>

需要 **Python 3.12+**。**不再需要 ffmpeg**——MP4 合并已经是内置的了。

```bash
git clone https://github.com/zhangjinluuu/bili-claw.git
cd bili-claw

# 装成全局命令（推荐，装完在任何目录都能用 bili-claw / bclaw）
uv tool install --editable .
uv tool update-shell          # 把 ~/.local/bin 加进 PATH，重开终端生效

# 或者用项目自己的虚拟环境
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
source .venv/bin/activate
```

自己打一份单文件产物：

```bash
bash packaging/build.sh       # → release/bclaw-<平台>.tar.gz
```

</details>

## 快速开始

```bash
# 第一次用：从浏览器取一份 Cookie，解锁 1080P+
bclaw --login

# 下载 1080P（本文档其余示例都用这个视频）
bclaw BV17wpp6SEe9 -q 1080

# 下载最高可用清晰度
bclaw BV17wpp6SEe9

# 先看看有哪些清晰度可选
bclaw BV17wpp6SEe9 --list-qualities

# 只看信息不下载
bclaw BV17wpp6SEe9 --info

# 下载多分P 的第 1~3 P
bclaw BV17wpp6SEe9 -p 1-3

# 只要音频
bclaw BV17wpp6SEe9 --audio-only
```

## 清晰度选择

`-q` 接受四种写法：

| 写法 | 含义 | 例子 |
|------|------|------|
| `max` | 当前账号能拿到的最高档 | `-q max`（默认） |
| `ask` | 列出**当前账号实际能下载**的档位，选完再下 | `-q ask` |
| 高度 | **不超过**该高度的最高可用档 | `-q 1080` → 有 1080P60 就给 1080P60 |
| `qn` 数字 | 精确请求某一档 | `-q 116` → 1080P 60帧 |

`-q ask` 的菜单只列你真的拿得到的流——把没权限的档位摆出来让人选、选完才发现
下不了，比不给选更糟。第一项是最好的那档，直接回车就等于 `-q max`：

```
$ bclaw BV17wpp6SEe9 -q ask
  ℹ 清晰度：交互选择——将列出当前账号实际可下载的档位

  ▶ P1 改写战局！多库替补造2球+造红牌 哈兰德破门 十人利物浦0比3曼城
   #  清晰度  编码       码率  分辨率
   1  1080P   avc   3990 kbps  1920×1080
   2  1080P   hevc  1581 kbps  1920×1080
   3  1080P   av1   1552 kbps  1920×1080
   4  720P    avc   1726 kbps  1280×720
   5  720P    hevc   775 kbps  1280×720
   6  720P    av1    760 kbps  1280×720
   7  480P    avc    818 kbps  852×480
   8  480P    hevc   441 kbps  852×480
   9  480P    av1    437 kbps  852×480
  10  360P    avc    637 kbps  640×360
  11  360P    hevc   297 kbps  640×360
  12  360P    av1    295 kbps  640×360
   请选择要下载的清晰度 (1): 2
  ✓ 已选择 1080P · 1920×1080 · 30fps · hevc · 1581 kbps
```

同一个 1080P 有 avc / hevc / av1 三种编码，码率差到 2.5 倍（3990 vs 1552 kbps）——
体积和兼容性怎么取舍见下方的「编码偏好」。

`ask` 需要交互式终端。在脚本或管道里它会立刻报错并告诉你可选的档位，而不是
挂在那里等输入——自动化命令卡住等按键是最难排查的一种失败。

常用档位：

| qn | 档位 | qn | 档位 |
|----|------|----|------|
| 127 | 8K 超高清 | 80 | 1080P 高清 |
| 126 | 杜比视界 | 74 | 720P 60帧 |
| 125 | HDR 真彩 | 64 | 720P 高清 |
| 120 | 4K 超清 | 32 | 480P 清晰 |
| 116 | 1080P 60帧 | 16 | 360P 流畅 |
| 112 | 1080P 高码率 | 6 | 240P 极速 |

### 档位不可用时怎么办

B站的行为是：**你请求一个档位，它返回它能给的，并且不告诉你降级了。**
bili-claw 会把实际返回的流和你的请求对比，然后：

```bash
# 默认：降级 + 醒目提示（这个账号没有大会员，8K 拿不到）
$ bclaw BV17wpp6SEe9 -q 4320
  ⚠ 请求的 4320P 不可用，已降级为 1080P

# 严格：不满足就报错退出，不产出任何文件
$ bclaw BV17wpp6SEe9 -q 4320 --on-unavailable fail
  ✗ 请求的 4320P 不可用，已降级为 1080P，且当前是严格模式
    去掉 --on-unavailable=fail 即可自动降级到可用档位；或改用该视频实际提供的档位
  （退出码 2 —— 这条片源最高就到 1080P，去开大会员也没用）
```

未登录时同样会如实说明挡在哪：

```bash
$ bclaw BV17wpp6SEe9 -q 1080          # 未提供登录态
  ⚠ 请求的 1080P 不可用，已降级为 480P；当前未登录（未登录上限 480P），且 1080P60 还需要大会员——先提供登录态（--cookie-file）
```

严格模式下，退出码会区分两种截然不同的原因，方便脚本决定下一步：

- **退出码 4**：片源里还有你能接受、却没给你的档位 → 该去登录 / 开大会员
- **退出码 2**：片源最高就到你现在拿到的这一档 → 换档位就行，登录也没用

注意 `-q 1080` 的语义是「不超过 1080P 里最高的那档」，不是「必须是 1080P」。所以
一个只提供到 720P 的视频，`-q 1080` 给出的也是 **4** 而不是 2——它确实有一档
（720P）是你愿意要的，只是被登录挡住了。只有片源本身就只到 480P 时才是 2。

判据是**片源实际提供了哪些档位**，而不是「你去登录试试看」：把「本来就没有这个
清晰度」报成「需要登录」，会让人白折腾一趟登录甚至开个会员，回来发现画质没变。

### 编码偏好

同档位可能有多种编码，体积和兼容性不同（AV1 最小、HEVC 次之、AVC 兼容性最好）：

```bash
bclaw BV17wpp6SEe9 --codec avc,hevc     # 优先 AVC，其次 HEVC
bclaw BV17wpp6SEe9 --codec-compat       # 等价于 --codec avc,hevc，最大兼容性
```

## 登录态（1080P+ 的前提）

**未登录最高只能拿到 480P**。1080P 及以上需要登录，4K/8K/HDR 还需要大会员。

### 一键取 Cookie（推荐）

只要你在本机浏览器里登录过 B站，就不用去开发者工具里翻：

```bash
bclaw --login
```

```
✓ 从 Google Chrome · Default · 29 个字段 读到 B站 Cookie
✓ 已写入：/Users/you/.biliclaw/cookies.txt（权限 600，这是凭据别外传）
✓ 验证通过：XXXX（UID xxxxxxxx，非大会员）
  → 该账号最高可下载：1080P / 1080P60（4K / 8K / HDR 需要大会员）
```

它会从 **Chrome / Edge / Brave / Chromium / Vivaldi / Firefox** 里找一份当前有效的
B站 Cookie，写到默认路径（权限 `600`），**并当场向B站验证一次**——所以上面那行
「验证通过」是真实结果，不是「文件写好了」的另一种说法。

#### 各平台支持情况

| | Chromium 系（Chrome / Edge / Brave / Chromium / Vivaldi） | Firefox |
|---|---|---|
| **macOS** | ✅ 需要钥匙串授权（首次可能弹一次框） | ✅ **推荐**，无需授权 |
| **Linux** | ✅ gnome-keyring；取不到就用 Chromium 的固定口令 | ✅ **推荐** |
| **Windows** | ⚠️ 仅旧版加密；**Chrome / Edge 127+ 的 App-Bound 加密不支持** | ✅ **推荐** |

**三个平台的加密方式互不相同**，不是同一个东西换个名字：macOS 是钥匙串口令派生
AES-128、Linux 用同一套但**迭代次数不同**、Windows 是 DPAPI 保护的 AES-256 密钥。
所以「在我机器上能用」不代表在别的平台也能用。

> **Windows 用户注意**：如果 Chrome / Edge 报「Cookie 用了 App-Bound 加密，本程序解不开」，
> **去浏览器重新登录是没用的**——Cookie 明明存着，只是密钥只有浏览器自己拿得到。这是
> Chrome 127+ 的新方案，本工具不做（正规解法只有从浏览器进程里读内存，那和恶意软件的手法
> 没有区别）。请改用 Firefox，或用手动方式提供 Cookie。

#### 完全不用浏览器：手机扫码登录

```bash
bclaw --login --qrcode
```

```
▄▄▄▄▄▄▄  ▄▀▄█▀▀ ███▄▀▄▄█ ▄ ▄ ▄█▀ ▀▄█▀█▄▄█ █▀▀▀▀▀█
█ ███ █  █▄ ██▀ █▄█ █ ▄▀▀██▀▄▀▄▄▄▀▄██▄▄ █▀ █ ███ █
█ ▀▀▀ █  █▀▄██▀▄█▀▀█▄ ▀█▀▀▀█▄▄▀▀▄▀▄▄██▀▄   █ ▀▀▀ █
▀▀▀▀▀▀▀  █ █▄█ █ █ ▀ ▀ █ ▀ █ █▄█▄█ ▀▄▀▄█▄▀ ▀▀▀▀▀▀▀
   …（终端里画出的二维码）
用 B站 App 扫这个码，再在手机上点「确认登录」（180 秒内有效）
✓ 登录成功：XXXX（UID xxxxxxxx，非大会员）
✓ 已写入：/Users/you/.biliclaw/cookies.txt（权限 600，这是凭据别外传）
  → 该账号最高可下载：1080P / 1080P60
```

这条路**完全不碰浏览器**：不需要钥匙串授权，也不受 Chrome 127+ App-Bound 加密的限制，
在服务器、容器、刚装好的机器上都能用——只要有终端和手机。

两个和浏览器那条路不同的地方，都是刻意的：

- **先验证再落盘。** 浏览器那条路只能先存后验（凭据已经从浏览器里捞出来了），而这里的
  Cookie 还在内存里——B站不认，就一个字节都不写，不会在磁盘上留一个「看着像登录态、
  实际是废的」文件，等下次下载才发现。
- **二维码在本机生成。** 码里编的是这次登录的 `qrcode_key`，谁的手机先扫完，被扫码的
  这一端就存下谁的账号——把它发给任何一个二维码生成服务，等于把登录会话交出去。

也可以和下载连起来一条命令做完：

```bash
bclaw BV17wpp6SEe9 --login -q 1080      # 取完 Cookie 直接下这一条
bclaw --login --cookie-file ~/bili-cookies.txt   # 写到别处
bclaw --login --browser firefox  # 指定浏览器（默认 auto，按 Chrome 系优先）
bclaw BV17wpp6SEe9 --login --qrcode      # 扫码登录，然后直接下这一条
```

想完全避开授权弹窗就用 **Firefox**：它的 `cookies.sqlite` 在三个平台上**都是明文**，
没有钥匙串、没有 DPAPI，差别只在 profile 目录的位置。

> Safari 的 Cookie 是苹果私有格式且要「完全磁盘访问权限」，**不支持**（它也只有 macOS 上有）——
> 遇到 `--browser safari` 会直接说明原因，而不是静默失败。

### 手动提供 Cookie

一键取不适用时（比如 B站登录在另一台机器上），四条路仍然都在：

```bash
# 方式一：放到默认路径，之后所有命令自动带上，不用加参数（推荐）
mkdir -p ~/.biliclaw && chmod 700 ~/.biliclaw
printf '%s' 'SESSDATA=粘贴到这里; bili_jct=...' > ~/.biliclaw/cookies.txt
chmod 600 ~/.biliclaw/cookies.txt

# 方式二：临时指定某个文件
bclaw BV17wpp6SEe9 --cookie-file ~/bili-cookies.txt

# 方式三：直接传字符串
bclaw BV17wpp6SEe9 --cookie "SESSDATA=xxx; bili_jct=yyy"

# 方式四：环境变量
export BILICLAW_COOKIE="SESSDATA=xxx"
bclaw BV17wpp6SEe9
```

四种方式按 `--cookie` > `--cookie-file` > 环境变量 > 默认路径 的顺序生效，
默认路径同时兼容 `~/.bilidl/cookies.txt`（见下）。

> 改名（bilidl → BiliClaw）之前放在 `~/.bilidl/cookies.txt` 的文件**仍然会读**，
> 只是排在新路径之后。想搬就搬，不搬也能用；两个路径都存在时以 `~/.biliclaw` 为准。
> 环境变量同理，旧的 `BILIDL_COOKIE` 依旧生效。

> ⚠️ Cookie 等同于账号凭据。bili-claw 只把它放进请求头，不写入日志、不写入产物文件；
> `--debug` 输出里的 Cookie 也会自动脱敏。但仍然请勿把 Cookie 文件提交到版本库，
> 并保持 `chmod 600`（仅本人可读）。

放好之后用 `bili-claw --check` 确认登录态**真的**生效了——它会显示账号名与大会员状态，
并告诉你这个账号最高能下到多高；如果 Cookie 已过期，会直接说「多半是 SESSDATA 过期了」
并给出退出码 4，而不是含糊地报个网络错误。

## 常用参数

```
-h, --help                显示完整帮助
-q, --quality TEXT        清晰度：max / ask / 1080 / 720 / 80 / 116 …
--on-unavailable TEXT     档位不可用：degrade（默认）| fail
--codec TEXT              编码偏好，如 avc,hevc
--audio-quality TEXT      音轨：auto（默认）| best | 30280 …
--audio-only              只下载音频，不转码（AAC 存为 .m4a，Hi-Res 无损存为 .flac）
--container TEXT          容器：mp4（默认）| mkv
-o, --output PATH         输出根目录（默认 downloads）
--template TEXT           命名模板（默认 {up}/{title}）
-p, --page TEXT           分P：3 / 1-3 / 1,3,5 / all
--cookie-file PATH        Cookie 文件
--login                   从本机浏览器取 B站 Cookie，写入默认路径并当场验证
--qrcode                  配合 --login：不碰浏览器，在终端里画二维码用 B站 App 扫
--browser TEXT            指定浏览器：auto（默认）| chrome | edge | brave | chromium | vivaldi | firefox | safari
--concurrency INT         单文件并发分片数（默认 4）
--limit-rate TEXT         全局限速，如 1M / 500K
--overwrite               覆盖已存在文件（默认跳过）
--keep-parts              合并后保留 .m4s 分片
--no-merge                只下载分片，不合并
-Q, --quiet               不显示进度条
--debug                   输出调试日志
--version                 显示版本
--logout                  登出：只删掉本机保存的登录态，程序留着
--uninstall               卸载 bclaw：删掉程序本体与本机保存的登录态
-y, --yes                 配合 --uninstall：不再询问，直接删
```

敲错选项时不会只丢一句 `No such option`——程序会把这个错误和**完整的帮助**一起打出来，
省掉"哦，那我再敲一次 `--help`"这一轮。

### 登出

```bash
bclaw --logout         # 删掉本机保存的登录态，程序留着
```

删的是 `--login` 写下的那份 Cookie（`~/.biliclaw/cookies.txt`），以及改名前的
`~/.bilidl/cookies.txt`——两份都删，因为程序在新路径不存在时会**回退**去读旧的，
只删一份等于没登出。

删完不会问第二遍：这条命令只有一种解释，而误删的代价是重新登录一次。**也不报假成功**：
哪份文件没删掉会如实说明并以退出码 7 结束。

如果 shell 里还设着 `BILICLAW_SESSDATA` 之类的环境变量，程序会提醒你——环境变量比文件
优先，光删文件，下一次下载照样带着那份凭据。

### 卸载

```bash
bclaw --uninstall      # 先列出要删的东西，再问一次；确认后才动手
bclaw --uninstall --yes   # 脚本里用这个跳过询问
```

删三样：程序本体（`~/.local/bin/bclaw`）、别名软链（`~/.local/bin/bili-claw`），
以及 `~/.biliclaw/`（`--login` 存下的 Cookie——它是能直接冒充你身份的凭据，
程序都卸了更不该留在机器上）。

删除不可撤销，所以默认先列出清单再让你确认。Windows 不允许删除正在运行的程序，
那一条会如实报告失败、退出码 7，关掉窗口后手动删掉即可——**不会**假报成功。

### Cookie 失效了会怎样

如果提供的 Cookie 已经不是登录态（最常见的是 SESSDATA 过期），程序**不会**悄悄
以未登录身份继续下载——那会让你以为拿到了 1080P、其实只有 480P。它会直接停下：

```
  ✗ Cookie 已失效（来源：/Users/you/.biliclaw/cookies.txt）——B站不认为这是登录态
  → 多半是 SESSDATA 过期了。重新登录B站并复制一份新的 Cookie 覆盖它；
```

退出码是 **4**。想继续用未登录身份下载，把 Cookie 去掉即可（最高 480P）。

失效 Cookie 的实际表现值得一提：服务端拒掉凭据后 `playurl` 会返回
`quality=0`、`dash=null`，看起来完全像"这个视频没有可用的视频流"。不先查登录态
的话，用户就会被引去怀疑视频本身，而真正的问题在自己的凭据上。

### 命名模板

`--template` 渲染出的相对路径会拼到 `--output` 下面，扩展名自动添加。

可用变量：`{title}` `{bvid}` `{aid}` `{cid}` `{p}` `{p_name}`/`{pname}` `{up}` `{mid}` `{qn}` `{quality}` `{codec}` `{date}` `{datetime}`

支持 `{p:02d}` 这类格式说明符。

```bash
# 默认：downloads/{UP主}/{标题}.mp4
bclaw BV17wpp6SEe9

# 按 UP 主+分P 归档
bclaw BV17wpp6SEe9 --template "{up}/{title}/P{p:02d}"

# 扁平化，带 BV 号
bclaw BV17wpp6SEe9 -o ~/Videos --template "{title} [{bvid}]"
```

多分P 下载时，如果模板里没用到 `{p}`，程序会自动补上 `_P01` 这样的后缀并明确告知，
而不是让分P 互相覆盖。

## 退出码

便于脚本判断失败原因：

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 部分失败（多分P 时有的成功有的失败） |
| 2 | 参数/用法错误、视频不存在、片源最高只到已获得的档位（登录也没用） |
| 3 | 网络错误、接口返回结构异常 |
| 4 | 需要登录 / 权限不足（含大会员专享） |
| 5 | 触发风控（HTTP 412 等） |
| 6 | 运行环境有问题（合并内核自检失败——这是安装包本身的问题，请重装或反馈） |
| 7 | 磁盘或文件系统错误 |

## 断点续传

下载中的分片保存在 `<目标文件>.part`，进度记录在同名 `.part.json`。
中断（Ctrl-C、断网、关机）后**重新运行完全相同的命令**即可从断点继续：

```bash
$ bclaw BV17wpp6SEe9 -q 1080
^C
已中断。已下载的分片保存在 .part 文件中，重新运行同一命令会从断点继续。

$ bclaw BV17wpp6SEe9 -q 1080     # 从断点继续
```

「下载到哪了」以 `.part.json` 为准，**不是**看 `.part` 的大小——`.part` 是按完整长度预分配的，
「看着够大」并不代表内容完整。所以如果 `.part` 被截断或损坏，工具会放弃续传、整体重下：
多花一次下载时间，换来的是绝不产出一个静默损坏、播放器打不开的文件。

## 开发

```bash
uv pip install -e ".[dev]"
.venv/bin/python -m pytest -v
```

打一份单文件产物（PyInstaller 不能交叉编译，每个平台各跑一次）：

```bash
bash packaging/build.sh       # → release/bclaw-<平台>.tar.gz
```

项目结构：

```
src/biliclaw/
  cli.py         命令行入口、下载编排
  api.py         B站接口客户端（view / playurl / nav）
  wbi.py         WBI 签名（w_rid 计算）
  cookies.py     Cookie 加载、校验、脱敏
  browser.py     从本机浏览器提取 Cookie（--login，三平台）
  qrlogin.py     扫码登录：要二维码、轮询状态、在终端里画码（--login --qrcode）
  models.py      接口响应数据模型
  quality.py     清晰度协商与降级判定
  downloader.py  分片并发下载与断点续传
  mp4.py         纯 Python 的 MP4 读写内核（盒子解析 / 样本表 / 交织合并）
  muxer.py       合并编排：MP4 走内置内核，MKV 才回退 ffmpeg
  selftest.py    --check 用的内置合并样本
  naming.py      模板渲染与文件名净化
  progress.py    进度展示
  errors.py      错误分类与退出码
packaging/       PyInstaller 配置、构建脚本、安装脚本
docs/需求文档.md   需求规格说明
docs/落地计划.md   零依赖 / 跨平台的落地计划与实测记录
```

## 免责声明

仅供个人学习与备份使用。请遵守 B站用户协议与相关法律法规，不要用于批量抓取、
二次分发或任何商业用途。下载的内容版权归原作者所有。

## 许可证

[MIT](LICENSE)
