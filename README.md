# bilidl — B站视频下载器

把 B站视频下载到本地，支持清晰度选择、登录态解锁 1080P+、DASH 音视频并发下载与 ffmpeg 无损合并。

> **先说实话**：如果你只是想下载 B站视频，[yt-dlp](https://github.com/yt-dlp/yt-dlp) 已经做得非常完整了。
> 这个项目的价值在于把「清晰度选择」这件事做对——B站会静默降级你的请求，绝大多数工具只告诉你
> 它下到了什么，而不告诉你**为什么没拿到你要的那一档**。bilidl 的原则是：绝不静默降级。

---

## 特性

- **清晰的清晰度语义**：`-q 1080` 的含义是"不超过 1080P 的最高可用档位"，而不是"必须是 1080P"
- **降级必须告知**：请求 4K 但账号没有大会员时，会明确告诉你实际拿到了什么、为什么
- **严格模式**：`--on-unavailable fail` 让档位不匹配直接报错退出，适合脚本
- **登录态解锁**：支持 Cookie 文件，1080P / 1080P+ / 4K / 8K 按账号权限如实反映
- **DASH 并发下载**：视频流多分片并发 + 断点续传，中断后重跑同一命令即可继续
- **CDN 链接过期自动换链**：B站下载链接约 2 小时失效，遇 403 自动重新请求 playurl
- **无损合并**：只做 remux（`-c copy`），不重新编码，画质音质零损失
- **机器可用的退出码**：0/1/2/3/4/5/6/7 区分成功、部分失败、参数错误、网络、权限、风控、缺依赖、磁盘

## 安装

需要 **Python 3.12+** 与 **ffmpeg**。

```bash
# macOS
brew install ffmpeg

# 安装 bilidl
cd bilibili_downloader
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
source .venv/bin/activate
```

验证环境（会检查 ffmpeg 与登录态）：

```bash
bilidl --check
```

## 快速开始

```bash
# 下载最高可用清晰度
bilidl "https://www.bilibili.com/video/BV1xx411c7mD"

# 只要 720P
bilidl BV1xx411c7mD -q 720

# 先看看有哪些清晰度可选
bilidl <URL> --list-qualities

# 只看信息不下载
bilidl <URL> --info

# 下载多分P 的第 1~3 P
bilidl <URL> -p 1-3

# 只要音频
bilidl <URL> --audio-only
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
$ bilidl <URL> -q ask
  #  清晰度  编码      码率  分辨率
  1  480P    avc   475 kbps  852×480
  2  480P    hevc  253 kbps  852×480
  3  480P    av1   227 kbps  852×480
  4  360P    avc   330 kbps  640×360
  请选择要下载的清晰度 (1): 4
  ✓ 已选择 360P · 640×360 · 30fps · avc · 330 kbps
```

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
bilidl 会把实际返回的流和你的请求对比，然后：

```bash
# 默认：降级 + 醒目提示
$ bilidl <URL> -q 1080
  ⚠ 请求的 1080P 不可用，已降级为 480P；当前未登录（未登录上限 480P），且 1080P60 还需要大会员——先提供登录态（--cookie-file）

# 严格：不满足就报错退出，不产出任何文件
$ bilidl <URL> -q 1080 --on-unavailable fail
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
bilidl <URL> --codec avc,hevc     # 优先 AVC，其次 HEVC
bilidl <URL> --codec-compat       # 等价于 --codec avc,hevc，最大兼容性
```

## 登录态（1080P+ 的前提）

**未登录最高只能拿到 480P**。1080P 及以上需要登录，4K/8K/HDR 还需要大会员。
bilidl 不会代替你登录，你需要自己提供 Cookie：

1. 浏览器登录 B站
2. 打开开发者工具 → Application → Cookies → `https://www.bilibili.com`
3. 复制整条 Cookie 字符串（至少要含 `SESSDATA`），或导出为 Netscape 格式

```bash
# 方式一：放到默认路径，之后所有命令自动带上，不用加参数（推荐）
mkdir -p ~/.bilidl && chmod 700 ~/.bilidl
printf '%s' 'SESSDATA=粘贴到这里; bili_jct=...' > ~/.bilidl/cookies.txt
chmod 600 ~/.bilidl/cookies.txt

# 方式二：临时指定某个文件
bilidl <URL> --cookie-file ~/bili-cookies.txt

# 方式三：直接传字符串
bilidl <URL> --cookie "SESSDATA=xxx; bili_jct=yyy"

# 方式四：环境变量
export BILIDL_COOKIE="SESSDATA=xxx"
bilidl <URL>
```

四种方式按 `--cookie` > `--cookie-file` > 环境变量 > 默认路径 的顺序生效。

> ⚠️ Cookie 等同于账号凭据。bilidl 只把它放进请求头，不写入日志、不写入产物文件；
> `--debug` 输出里的 Cookie 也会自动脱敏。但仍然请勿把 Cookie 文件提交到版本库，
> 并保持 `chmod 600`（仅本人可读）。

放好之后用 `bilidl --check` 确认登录态**真的**生效了——它会显示账号名与大会员状态，
并告诉你这个账号最高能下到多高；如果 Cookie 已过期，会直接说「多半是 SESSDATA 过期了」
并给出退出码 4，而不是含糊地报个网络错误。

## 常用参数

```
-q, --quality TEXT        清晰度：max / ask / 1080 / 720 / 80 / 116 …
--on-unavailable TEXT     档位不可用：degrade（默认）| fail
--codec TEXT              编码偏好，如 avc,hevc
--audio-quality TEXT      音轨：auto（默认）| best | 30280 …
--audio-only              只下载音频，输出 .m4a（不转码）
--container TEXT          容器：mp4（默认）| mkv
-o, --output PATH         输出根目录（默认 downloads）
--template TEXT           命名模板（默认 {up}/{title}）
-p, --page TEXT           分P：3 / 1-3 / 1,3,5 / all
--cookie-file PATH        Cookie 文件
--concurrency INT         单文件并发分片数（默认 4）
--limit-rate TEXT         全局限速，如 1M / 500K
--overwrite               覆盖已存在文件（默认跳过）
--keep-parts              合并后保留 .m4s 分片
--no-merge                只下载分片，不调用 ffmpeg
-Q, --quiet               不显示进度条
--debug                   输出调试日志
```

### 命名模板

`--template` 渲染出的相对路径会拼到 `--output` 下面，扩展名自动添加。

可用变量：`{title}` `{bvid}` `{aid}` `{p}` `{p_name}` `{up}` `{mid}` `{qn}` `{quality}` `{date}` `{datetime}`

支持 `{p:02d}` 这类格式说明符。

```bash
# 默认：downloads/{UP主}/{标题}.mp4
bilidl <URL>

# 按 UP 主+分P 归档
bilidl <URL> --template "{up}/{title}/P{p:02d}"

# 扁平化，带 BV 号
bilidl <URL> -o ~/Videos --template "{title} [{bvid}]"
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
| 6 | 缺少依赖（ffmpeg / ffprobe） |
| 7 | 磁盘或文件系统错误 |

## 断点续传

下载中的分片保存在 `<目标文件>.part`，进度记录在同名 `.part.json`。
中断（Ctrl-C、断网、关机）后**重新运行完全相同的命令**即可从断点继续：

```bash
$ bilidl <URL> -q 1080
^C
已中断。已下载的分片保存在 .part 文件中，重新运行同一命令会从断点继续。

$ bilidl <URL> -q 1080     # 从断点继续
```

「下载到哪了」以 `.part.json` 为准，**不是**看 `.part` 的大小——`.part` 是按完整长度预分配的，
「看着够大」并不代表内容完整。所以如果 `.part` 被截断或损坏，工具会放弃续传、整体重下：
多花一次下载时间，换来的是绝不产出一个静默损坏、播放器打不开的文件。

## 开发

```bash
uv pip install -e ".[dev]"
.venv/bin/python -m pytest -v
```

项目结构：

```
src/bilidl/
  cli.py         命令行入口、下载编排
  api.py         B站接口客户端（view / playurl / nav）
  wbi.py         WBI 签名（w_rid 计算）
  cookies.py     Cookie 加载、校验、脱敏
  models.py      接口响应数据模型
  quality.py     清晰度协商与降级判定
  downloader.py  分片并发下载与断点续传
  muxer.py       ffmpeg 无损合并
  naming.py      模板渲染与文件名净化
  progress.py    进度展示
  errors.py      错误分类与退出码
docs/需求文档.md   需求规格说明
```

## 免责声明

仅供个人学习与备份使用。请遵守 B站用户协议与相关法律法规，不要用于批量抓取、
二次分发或任何商业用途。下载的内容版权归原作者所有。
