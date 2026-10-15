"""哔哩娃娃机 / BiliClaw —— 命令行入口。

投币，出爪，把喜欢抓回家。

对应需求：FR-01 输入解析、FR-02 信息展示、FR-03 清晰度选择、FR-04 下载、
FR-05 合并、FR-07 命名、FR-09 登录态、FR-11 进度与退出码、FR-12 错误提示。

用法示例::

    bclaw --login                                  # 一键从浏览器取登录态（推荐先跑这个）
    bili-claw "https://www.bilibili.com/video/BV1xx411c7mD"
    bclaw BV1xx411c7mD -q 720
    bclaw <URL> -q 1080 --cookie-file ~/bili-cookies.txt
    bclaw <URL> --list-qualities
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.padding import Padding
from rich.prompt import Prompt
from rich.table import Table

from . import __version__, browser, quality
from .api import BiliClient
from .cookies import DEFAULT_COOKIE_PATH, is_logged_in, redact, resolve_cookie
from .downloader import DEFAULT_CONCURRENCY, StreamDownloader, parse_rate
from .errors import (
    AuthError,
    BiliError,
    ExitCode,
    LinkExpiredError,
    NetworkError,
    QualityUnavailableError,
    UsageError,
)
from .models import Page, PlayUrlData, VideoInfo
from .muxer import choose_container, find_ffmpeg, merge, verify_output
from .naming import TEMPLATE_FIELDS, render_path, template_fields, validate_template
from .progress import ProgressReporter
from .selftest import check_merge

app = typer.Typer(
    add_completion=False,
    help=(
        "哔哩娃娃机 / BiliClaw —— 投币，出爪，把喜欢抓回家。\n\n"
        "支持清晰度选择、Cookie 登录下载 1080P+、DASH 并发下载与内置的无损合并。\n\n"
        "合并由本程序自己完成，不需要 ffmpeg 等任何外部程序。"
    ),
)

err_console = Console(stderr=True)

BVID_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})")
AID_PATTERN = re.compile(r"(?<![A-Za-z])av(\d{1,12})", re.IGNORECASE)
SHORT_LINK_PATTERN = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)
PAGE_QUERY_PATTERN = re.compile(r"[?&]p=(\d+)")

DEFAULT_TEMPLATE = "{up}/{title}"

#: 只有预计超过一秒的合并才值得显示进度条。按实测吞吐（129 MB / 0.25 s，约 500 MB/s）
#: 折算，这条线大致落在 512 MB——17 分钟的 1080P 远在其下，4K 长片会跨过去。
#: 短操作上闪一下的进度条比没有更糟：用户会以为界面出了毛病。
MERGE_PROGRESS_THRESHOLD = 512 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 输入解析（FR-01）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Target:
    bvid: str | None = None
    aid: int | None = None
    page: int | None = None


def parse_target_text(text: str) -> Target:
    """从链接、BV 号或 av 号里解析出稿件标识与可选分P。"""
    raw = text.strip()
    if not raw:
        raise UsageError("请提供视频链接、BV 号或 av 号")

    page_match = PAGE_QUERY_PATTERN.search(raw)
    page = int(page_match.group(1)) if page_match else None

    bvid_match = BVID_PATTERN.search(raw)
    if bvid_match:
        return Target(bvid=bvid_match.group(1), page=page)

    aid_match = AID_PATTERN.search(raw)
    if aid_match:
        return Target(aid=int(aid_match.group(1)), page=page)

    raise UsageError(
        f"无法从输入中识别出视频：{text}",
        hint="支持的形式：https://www.bilibili.com/video/BV1xx411c7mD、BV1xx411c7mD、av12345、https://b23.tv/xxxx",
    )


async def resolve_target(client: BiliClient, text: str) -> Target:
    """解析输入；遇到 b23.tv 短链先跟一次跳转。"""
    raw = text.strip()
    short_match = SHORT_LINK_PATTERN.search(raw)
    if not short_match:
        return parse_target_text(raw)

    try:
        response = await client.http.get(short_match.group(0), follow_redirects=True)
    except httpx.HTTPError as exc:
        # 展开短链必须联网。把"网断了"说成"链接无法识别"会让人去查链接本身，
        # 方向就错了——这里如实报网络问题，并给出不需要联网的替代做法。
        raise NetworkError(
            f"无法展开短链：{short_match.group(0)}",
            hint="短链需要联网解析；也可以直接使用完整的视频链接或 BV 号",
        ) from exc

    resolved = str(response.url)
    if BVID_PATTERN.search(resolved) or AID_PATTERN.search(resolved):
        return parse_target_text(resolved)
    raise UsageError(
        f"短链跳转后仍无法识别视频：{short_match.group(0)}",
        hint="请改用完整的视频链接",
    )


def parse_page_spec(spec: str, total: int) -> list[int]:
    """解析 ``-p``：单个序号、``1-3``、``1,3,5`` 或 ``all``。"""
    text = spec.strip().lower()
    if text in {"all", "*"}:
        return list(range(1, total + 1))

    indexes: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_text, _, end_text = chunk.partition("-")
            start = int(start_text) if start_text.strip().isdigit() else 1
            end = int(end_text) if end_text.strip().isdigit() else total
            indexes.extend(range(start, end + 1))
        elif chunk.isdigit():
            indexes.append(int(chunk))
        else:
            raise UsageError(
                f"无法解析分P表达式：{spec}",
                hint="示例：-p 3 / -p 1-3 / -p 1,3,5 / -p all",
            )

    ordered = sorted(set(indexes))
    invalid = [index for index in ordered if index < 1 or index > total]
    if invalid:
        raise UsageError(
            f"分P序号超出范围：{', '.join(str(i) for i in invalid)}",
            hint=f"该稿件共有 {total} 个分P",
        )
    return ordered or [1]


def select_pages(info: VideoInfo, page_option: str | None, url_page: int | None) -> tuple[list[Page], int]:
    """决定下载哪些分P。优先级：``-p`` > 链接里的 ``?p=`` > 第 1 P。"""
    pages = info.page_list()
    if page_option:
        indexes = parse_page_spec(page_option, len(pages))
        return [pages[index - 1] for index in indexes], indexes[0]

    wanted = url_page or 1
    if wanted < 1 or wanted > len(pages):
        raise UsageError(
            f"链接里的分P序号超范围：p={wanted}",
            hint=f"该稿件共有 {len(pages)} 个分P",
        )
    return [pages[wanted - 1]], wanted


# --------------------------------------------------------------------------- #
# 展示（FR-02）
# --------------------------------------------------------------------------- #


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def print_video_info(
    console: Console,
    info: VideoInfo,
    *,
    logged_in: bool,
    cookie_source: str | None,
    pages: list[Page],
) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="bold cyan", justify="right")
    table.add_column()

    table.add_row("标题", info.title or "(无标题)")
    table.add_row("BV 号", f"{info.bvid}　(av{info.aid})" if info.bvid else f"av{info.aid}")
    table.add_row("UP 主", f"{info.owner.name}　(UID {info.owner.mid})")
    table.add_row("时长", format_duration(info.duration))
    if len(info.page_list()) > 1:
        table.add_row("分P", f"共 {len(info.page_list())} P")
    table.add_row("发布", time.strftime("%Y-%m-%d %H:%M", time.localtime(info.pubdate)) if info.pubdate else "未知")

    if logged_in:
        table.add_row("登录态", f"[green]已登录[/green]（来源：{cookie_source}）")
    else:
        table.add_row("登录态", "[yellow]未登录[/yellow]　最高只能下载 480P")

    if len(pages) <= 5:
        table.add_row("本次下载", "、".join(f"P{page.page}" for page in pages))
    else:
        table.add_row("本次下载", f"P{pages[0].page} ~ P{pages[-1].page}（共 {len(pages)} P）")

    console.print(table)


def _tier_reason(tier: quality.PermissionTier, logged_in: bool) -> str:
    if tier == "vip":
        return "需要大会员" + ("" if logged_in else "（且当前未登录）")
    if tier == "login":
        return "需要登录" + ("" if logged_in else "　← 当前未登录，提供 Cookie 即可解锁")
    return "当前账号不可用"


def print_qualities(
    console: Console,
    data: PlayUrlData,
    streams: list[quality.VideoStream],
    *,
    preference: tuple[str, ...],
    logged_in: bool,
) -> None:
    """``--list-qualities``：如实区分"视频提供"与"当前账号能拿"。"""
    console.print()
    console.print("[bold]当前账号实际可下载[/bold]")
    if streams:
        for line in quality.describe_available(streams, preference):
            console.print(f"  [green]✓[/green] {line}")
    else:
        console.print("  [yellow]没有可用的 DASH 视频流[/yellow]")

    catalog = sorted({qn for qn in (data.accept_quality or []) if qn in quality.QUALITY_NAMES}, reverse=True)
    if catalog:
        granted = {stream.qn for stream in streams}
        console.print()
        console.print("[bold]该视频提供的全部档位[/bold]")
        for qn in catalog:
            name = quality.QUALITY_NAMES[qn]
            if qn in granted:
                console.print(f"  [green]✓[/green] {name}（qn={qn}）")
            else:
                reason = _tier_reason(quality.required_tier(qn), logged_in)
                console.print(f"  [dim]· {name}（qn={qn}）　{reason}[/dim]")
    console.print()


# --------------------------------------------------------------------------- #
# 下载流程
# --------------------------------------------------------------------------- #


@dataclass
class Options:
    url: str
    quality: str = "max"
    on_unavailable: str = "degrade"
    codec: str | None = None
    codec_compat: bool = False
    audio_quality: str = "auto"
    audio_only: bool = False
    container: str = "mp4"
    output: Path = field(default_factory=lambda: Path("downloads"))
    template: str = DEFAULT_TEMPLATE
    page: str | None = None
    cookie: str | None = None
    cookie_file: Path | None = None
    concurrency: int = DEFAULT_CONCURRENCY
    limit_rate: str | None = None
    retries: int = 3
    list_qualities: bool = False
    info_only: bool = False
    check: bool = False
    login: bool = False
    browser: str = "auto"
    overwrite: bool = False
    keep_parts: bool = False
    no_merge: bool = False
    quiet: bool = False
    debug: bool = False


def _validate(options: Options) -> tuple[quality.QualityRequest, tuple[str, ...], float | None]:
    request = quality.parse_quality(options.quality)
    preference = quality.parse_codec_preference(options.codec, compat=options.codec_compat)
    validate_template(options.template)

    if options.on_unavailable not in {"degrade", "fail"}:
        raise UsageError(
            f"--on-unavailable 只接受 degrade 或 fail，收到：{options.on_unavailable}",
            hint="degrade（默认）自动降级并提示；fail 严格匹配，档位不可用时报错退出",
        )
    if options.container.lower() not in {"mp4", "mkv"}:
        raise UsageError(
            f"--container 只接受 mp4 或 mkv，收到：{options.container}",
        )

    rate = parse_rate(options.limit_rate)
    if options.limit_rate and rate is None:
        raise UsageError(
            f"无法解析限速值：{options.limit_rate}",
            hint="示例：--limit-rate 1M / 500K / 800000",
        )
    return request, preference, rate


async def _playurl(client: BiliClient, bvid: str, cid: int, qn: int) -> PlayUrlData:
    """取播放地址；WBI 签名失效（-403）时刷新密钥重试一次。"""
    try:
        return await client.playurl(bvid=bvid, cid=cid, qn=qn)
    except AuthError:
        logging.getLogger("biliclaw").debug("playurl 返回权限错误，刷新 WBI 后重试")
        client.invalidate_wbi()
        return await client.playurl(bvid=bvid, cid=cid, qn=qn)


async def _download_stream(
    downloader: StreamDownloader,
    *,
    urls: tuple[str, ...],
    size: int | None,
    dest: Path,
    refresh,
    on_progress,
    on_total,
    label: str,
) -> Path:
    """下载一条流；链接过期时调用 ``refresh`` 换链后带着同一份断点继续（FR-04.3）。"""
    try:
        return await downloader.download(urls, dest, on_progress=on_progress, on_total=on_total, total_hint=size)
    except LinkExpiredError:
        logging.getLogger("biliclaw").info("%s 的下载链接已过期，正在自动换链", label)
        fresh_urls, fresh_size = await refresh()
        return await downloader.download(
            fresh_urls, dest, on_progress=on_progress, on_total=on_total, total_hint=fresh_size
        )


def build_fields(info: VideoInfo, page: Page, *, qn: int, label: str, codec: str = "") -> dict[str, object]:
    published = info.pubdate or 0
    return {
        "title": info.title,
        "bvid": info.bvid,
        "aid": info.aid,
        "cid": page.cid,
        "p": page.page,
        "p_name": page.part,
        # 需求文档 FR-07.1 写的是 {pname}，README 里已经发布了 {p_name}——
        # 两个都给，免得改坏已经写进文档和脚本里的模板。
        "pname": page.part,
        "up": info.owner.name,
        "mid": info.owner.mid,
        "qn": qn,
        "quality": label,
        "codec": codec,
        "date": time.strftime("%Y%m%d", time.localtime(published)) if published else "00000000",
        "datetime": time.strftime("%Y%m%d_%H%M%S", time.localtime(published)) if published else "00000000_000000",
    }


def _can_prompt(console: Console) -> bool:
    """能否安全地向用户提问。

    管道、脚本、CI 里 stdin 常常已经关闭或不在终端上，此时 rich 的 Prompt 会抛
    EOFError，或者干脆一直等输入。一条自动化命令绝不能悄悄挂在那里，所以宁可
    提前报错。
    """
    try:
        return bool(console.is_terminal and sys.stdin.isatty())
    except (ValueError, OSError):
        # stdin 已关闭时 isatty() 会抛异常，一律当作不可交互。
        return False


def _prompt_quality(
    streams: list[quality.VideoStream],
    preference: tuple[str, ...],
    console: Console,
) -> quality.VideoStream:
    """``-q ask``：把"实际拿得到"的档位列出来让用户挑（对应 E2E-3）。

    菜单只列账号真的拿得到的流——把拿不到的档位摆出来让人选、选完才发现没权限，
    比不给选更糟。默认值是最高档，所以直接回车就等于 ``-q max``。
    """
    if not streams:
        raise QualityUnavailableError("该分P 没有可下载的视频流")

    menu = quality.sort_for_menu(streams, preference)

    if not _can_prompt(console):
        raise UsageError(
            "-q ask 需要交互式终端，当前环境无法读取键盘输入",
            hint=(
                "请直接指定档位，例如 -q 1080、-q 720 或 -q 80。"
                f"当前实际可选：{'、'.join(s.describe() for s in menu)}"
            ),
        )

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2, 0, 0))
    table.add_column("#", justify="right", style="dim")
    table.add_column("清晰度")
    table.add_column("编码")
    table.add_column("码率", justify="right")
    table.add_column("分辨率")
    for index, stream in enumerate(menu, start=1):
        table.add_row(
            str(index),
            stream.label,
            stream.codec,
            f"{stream.bandwidth / 1000:.0f} kbps" if stream.bandwidth else "—",
            f"{stream.width}×{stream.height}" if stream.width and stream.height else "—",
        )
    console.print(table)

    try:
        answer = Prompt.ask(
            "  请选择要下载的清晰度",
            choices=[str(i) for i in range(1, len(menu) + 1)],
            default="1",
            console=console,
            show_choices=False,
        )
    except EOFError:
        raise UsageError(
            "-q ask 读取输入时遇到文件结束（stdin 已关闭）",
            hint="请直接指定档位，例如 -q 1080",
        ) from None

    return menu[int(answer) - 1]


async def process_page(
    client: BiliClient,
    downloader: StreamDownloader,
    *,
    info: VideoInfo,
    page: Page,
    options: Options,
    request: quality.QualityRequest,
    preference: tuple[str, ...],
    template: str,
    logged_in: bool,
    progress: ProgressReporter,
    console: Console,
) -> Path | None:
    """下载并合并单个分P，返回产物路径（跳过时返回 ``None``）。"""
    bvid = info.bvid
    stream = None
    # 两种情况下都要用最高档去问：
    #   * 只要音频时——否则 `--on-unavailable fail` 会因为视频档位拿不到而中止一次
    #     纯音频下载，用户根本没要视频；
    #   * 交互选择时——playurl 只授予"请求档位降级后"的那一组流，只有请求最高档才能
    #     一次拿到该账号权限内的全部可选流，作为菜单数据。
    if options.audio_only or request.kind == "ask":
        qn = quality.MAX_QN
    else:
        qn = quality.qn_for_request(request)

    data = await _playurl(client, bvid, page.cid, qn)
    audio_streams = quality.to_audio_streams(data)

    if options.audio_only:
        pass
    elif request.kind == "ask":
        stream = _prompt_quality(quality.to_video_streams(data), preference, console)
        # 用户挑的就是实际拿到的那一档，不存在"降级"，也不必再走一遍协商逻辑。
        # 把 request 收敛成一个具体的 qn，后续换链（_refresh_video）就能原样复用。
        request = quality.QualityRequest(raw=request.raw, kind="qn", target=stream.qn)
        console.print(f"  [green]✓[/green] 已选择 {stream.describe()}")
    else:
        selection = quality.select_video(
            quality.to_video_streams(data),
            request,
            granted_qn=qn,
            preference=preference,
            on_unavailable=options.on_unavailable,
            accept_quality=data.accept_quality,
            logged_in=logged_in,
        )
        stream = selection.stream

        if selection.notice:
            style = "yellow" if selection.degraded else "dim"
            marker = "⚠" if selection.degraded else "ℹ"
            console.print(f"  [{style}]{marker} {selection.notice}[/{style}]")

    audio = quality.select_audio(audio_streams, want=options.audio_quality)
    if options.audio_only and audio is None:
        raise UsageError(f"P{page.page} 没有可用的音轨，无法只下载音频")

    container, warning = choose_container(
        options.container,
        video_codec=stream.codec if stream else None,
        audio_codec=audio.codecs if audio else None,
    )
    if warning:
        console.print(f"  [yellow]⚠ {warning}[/yellow]")

    # 目标路径与临时流文件（都放在最终目录里，断点文件才不会被当成垃圾清掉）
    # 纯音频时没有视频流，模板里的 {qn}/{quality} 就如实反映音轨规格。
    fields = build_fields(
        info,
        page,
        qn=stream.qn if stream else (audio.qn if audio else qn),
        label=stream.label if stream else (audio.label if audio else "音频"),
        codec=stream.codec if stream else (audio.codecs.split(".")[0] if audio else ""),
    )
    relative = render_path(template, fields)
    if options.audio_only:
        # Hi-Res 音轨是 FLAC，装进 .m4a 会误导播放器
        suffix = ".flac" if audio is not None and audio.codecs.startswith("flac") else ".m4a"
    else:
        suffix = f".{container}"
    dest = options.output / relative
    dest = dest.parent / (dest.name + suffix)
    stem = dest.parent / dest.stem

    if dest.exists() and not options.overwrite:
        console.print(f"  [dim]⏭ 已存在，跳过：{dest}[/dim]（加 --overwrite 可重新下载）")
        return None

    video_temp = stem.with_name(stem.name + ".video.m4s")
    audio_temp = stem.with_name(stem.name + ".audio.m4s")

    with progress:
        if options.audio_only:
            assert audio is not None
            task = progress.add(f"P{page.page} 音轨 {audio.label}", total=audio.size)
            await _download_stream(
                downloader,
                urls=audio.urls,
                size=audio.size,
                dest=dest,
                refresh=lambda: _refresh_audio(client, bvid, page.cid, qn, options, preference),
                on_progress=lambda n, t=task: progress.advance(t, n),
                on_total=lambda total, t=task: progress.set_total(t, total),
                label="音轨",
            )
            progress.complete(task)
            console.print(f"  [green]✓[/green] {dest}")
            return dest

        # 视频与音频各用一条进度条：接口给的 size 未必存在，要靠探测到的真实长度
        # 校正；两条流合用一条进度条，总数永远算不准（会显示成 8.8/? MB）。
        if options.overwrite or not video_temp.exists() or (
            stream.size and video_temp.stat().st_size != stream.size
        ):
            task = progress.add(f"P{page.page} 视频 {stream.label}", total=stream.size)
            await _download_stream(
                downloader,
                urls=stream.urls,
                size=stream.size,
                dest=video_temp,
                refresh=lambda: _refresh_video(client, bvid, page.cid, qn, request, preference, options, logged_in),
                on_progress=lambda n, t=task: progress.advance(t, n),
                on_total=lambda total, t=task: progress.set_total(t, total),
                label="视频流",
            )
            progress.complete(task)

        if audio is not None and (
            options.overwrite
            or not audio_temp.exists()
            or (audio.size and audio_temp.stat().st_size != audio.size)
        ):
            task = progress.add(f"P{page.page} 音轨 {audio.label}", total=audio.size)
            await _download_stream(
                downloader,
                urls=audio.urls,
                size=audio.size,
                dest=audio_temp,
                refresh=lambda: _refresh_audio(client, bvid, page.cid, qn, options, preference),
                on_progress=lambda n, t=task: progress.advance(t, n),
                on_total=lambda total, t=task: progress.set_total(t, total),
                label="音轨",
            )
            progress.complete(task)

    if options.no_merge:
        console.print(f"  [green]✓[/green] 已下载分片（--no-merge 未合并）：{video_temp}")
        return video_temp

    merge_total = sum(path.stat().st_size for path in (video_temp, audio_temp) if path.exists())
    slow_enough_to_show = not options.quiet and merge_total >= MERGE_PROGRESS_THRESHOLD
    with ProgressReporter(console, enabled=slow_enough_to_show) as bar:
        task = bar.add(f"P{page.page} 合并", total=merge_total)
        output = await merge(
            video=video_temp,
            audio=audio_temp if audio is not None else None,
            dest=dest,
            container=container,
            video_codec=stream.codec if stream else None,
            progress=lambda done, _total, t=task: bar.set_completed(t, done),
        )

    # FR-05.4：清理分片之前先确认产物真的完整。校验不过会抛错，于是下面的清理不会
    # 执行，分片保留下来供排查（FR-05.5）。
    warnings = verify_output(
        output,
        expect_video=True,
        expect_audio=audio is not None,
        expected_duration=float(page.duration) if page.duration else None,
    )

    if not options.keep_parts:
        video_temp.unlink(missing_ok=True)
        audio_temp.unlink(missing_ok=True)

    size_mb = output.stat().st_size / 1024 / 1024
    # 完成提示就是娃娃机的爪子落下那一刻：文件名是用户第一眼要找的东西，
    # 所以放在亮色主体里；完整路径与体积退到暗色，不抢视线。
    console.print(f"  🎰 抓到了：[green]{output.name}[/green]　[dim]{output.parent} · {size_mb:.1f} MB[/dim]")
    for message in warnings:
        console.print(f"  [yellow]⚠[/yellow] {message}")
    return output


async def _refresh_video(
    client: BiliClient,
    bvid: str,
    cid: int,
    qn: int,
    request: quality.QualityRequest,
    preference: tuple[str, ...],
    options: Options,
    logged_in: bool,
) -> tuple[tuple[str, ...], int | None]:
    data = await _playurl(client, bvid, cid, qn)
    selection = quality.select_video(
        quality.to_video_streams(data),
        request,
        granted_qn=qn,
        preference=preference,
        on_unavailable=options.on_unavailable,
        accept_quality=data.accept_quality,
        logged_in=logged_in,
    )
    return selection.stream.urls, selection.stream.size


async def _refresh_audio(
    client: BiliClient,
    bvid: str,
    cid: int,
    qn: int,
    options: Options,
    preference: tuple[str, ...],
) -> tuple[tuple[str, ...], int | None]:
    data = await _playurl(client, bvid, cid, qn)
    audio = quality.select_audio(quality.to_audio_streams(data), want=options.audio_quality)
    if audio is None:
        raise UsageError("换链后没有找到可用音轨")
    return audio.urls, audio.size


# --------------------------------------------------------------------------- #
# 环境自检
# --------------------------------------------------------------------------- #


async def run_check(options: Options, console: Console) -> int:
    """``--check``：确认运行环境与登录态，便于安装后第一时间发现问题（对应 E2E-9）。

    返回最具体的那一个退出码（FR-11），而不是笼统的失败码——脚本据此就能判断
    是该查网络，还是该重新登录。
    """
    code = ExitCode.OK

    def failed(candidate: ExitCode) -> None:
        """只让第一个失败原因占据退出码。

        各项检查是依次做的，后面发现问题时不该把前面的结论冲掉。
        顺序本身编码了优先级：网络 → 登录态。
        """
        nonlocal code
        if code is ExitCode.OK:
            code = candidate

    # 合并已经不依赖任何外部程序了，所以 ffmpeg 的有无**不再是失败**。这里只是如实
    # 报告能力边界：没有它照样能下 MP4，只是用不了 --container mkv。
    #
    # 光说"内置"是一句承诺，用户没法验证；所以现场拿内置样本合并一次。这一步是打包
    # 之后唯一能在用户机器上发现"这个构建的合并坏了"的地方——不然要等下完几百 MB
    # 才发现产物打不开。
    failure = check_merge()
    if failure:
        failed(ExitCode.MISSING_DEPENDENCY)
        console.print(f"[red]✗[/red] 合并：内置内核自检失败　[dim]{failure}[/dim]")
        console.print("  [dim]· 这是安装包本身的问题，不是你的环境问题——请重新安装，或反馈这条信息[/dim]")
    else:
        console.print("[green]✓[/green] 合并：内置纯 Python（自检通过）")

    ffmpeg = find_ffmpeg()
    if ffmpeg:
        console.print(f"  [dim]· ffmpeg {ffmpeg}——只有 --container mkv 会用到它[/dim]")
    else:
        console.print("  [dim]· 未装 ffmpeg——只有 --container mkv 会用到它，不影响 MP4[/dim]")

    cookies, source = resolve_cookie(explicit=options.cookie, cookie_file=options.cookie_file)
    has_cookie = is_logged_in(cookies)
    if has_cookie:
        console.print(f"[green]✓[/green] 登录态：已加载（来源：{source}）")
    else:
        console.print("[yellow]·[/yellow] 登录态：未登录，最高只能下载 480P（720P 及以上需要登录）")

    async with BiliClient(cookies=cookies, retries=options.retries) as client:
        nav = await client.probe_login()
        if nav is None:
            failed(ExitCode.NETWORK)
            console.print("[red]✗[/red] 无法访问 B站接口（网络不通或接口已变更）")
        elif nav.isLogin:
            # 用户真正想知道的是"登录之后我能下到多高"，而不是"cookie 读到了没有"，
            # 所以顺带把大会员状态翻出来——它决定了 4K/HDR 这一档到底开不开。
            tier = "大会员" if nav.is_vip() else "非大会员"
            console.print(f"[green]✓[/green] B站账号：{nav.uname}（UID {nav.mid}，{tier}）")
            ceiling = (
                "4K / 8K / HDR / 杜比（仍取决于片源是否提供）"
                if nav.is_vip()
                else "1080P / 1080P60（4K / 8K / HDR 需要大会员）"
            )
            console.print(f"  [dim]→ 该账号最高可下载：{ceiling}[/dim]")
        elif has_cookie:
            # 本地读到了 Cookie、B站却不认账：这不是"没登录"，而是登录态已经失效。
            # 含糊地说"接口可达"会让人去查网络，方向就错了。
            failed(ExitCode.AUTH)
            console.print(
                "[red]✗[/red] Cookie 已读到，但 B站不认为这是登录态——多半是 SESSDATA 过期了"
            )
            console.print("  [dim]→ 重新登录 B站并复制一份新的 Cookie（退出码 4）[/dim]")
        else:
            console.print("[yellow]·[/yellow] B站接口可达；未提供登录态（匿名最高 480P）")

    return int(code)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


async def _reject_expired_cookie(client: BiliClient, source: str) -> None:
    """带了 Cookie 却不是登录态，就明确说「登录已过期」并停下（FR-09）。

    不这么做的话，服务端拒绝了这个凭据后 ``playurl`` 会返回 ``quality=0``、
    ``dash=None``，用户看到的是"该视频没有可用的 DASH 视频流"——一句把矛头指向
    视频、而真正的问题在自己凭据上的话。更糟的情况是它悄悄以未登录身份下载，
    用户以为拿到了 1080P、其实只有 480P。

    与 ``--check`` 的判断保持一致：同样是"读到了 Cookie，但 B站不认"。
    """
    nav = await client.probe_login()
    if nav is None or nav.isLogin:
        # 探测不到就不断言：网络抖动不该被说成凭据过期
        return
    raise AuthError(
        f"Cookie 已失效（来源：{source}）——B站不认为这是登录态",
        hint=(
            "多半是 SESSDATA 过期了。重新登录B站并复制一份新的 Cookie 覆盖它；\n"
            "或者去掉这个 Cookie 改用未登录下载（最高 480P）"
        ),
    )


async def run_login(options: Options, console: Console) -> Path:
    """``--login``：从本机浏览器取 Cookie、落盘，并**当场验证它真的能登录**。

    验证这一步不能省。只把文件写下去就报成功，等于把"这份 Cookie 到底有没有用"
    推迟到下一次下载才暴露——而那时用户已经以为自己配好了，看到 480P 只会以为
    是片源的问题。

    返回写入的路径。
    """
    if options.cookie:
        raise UsageError(
            "--login 与 --cookie 不能同时使用",
            hint="--login 是要从浏览器取一份新的凭据；要用手工提供的凭据就直接写 --cookie，别加 --login",
        )

    target = options.cookie_file or DEFAULT_COOKIE_PATH
    extraction = browser.extract(options.browser)
    console.print(f"[green]✓[/green] 从 {extraction.describe()} 读到 B站 Cookie")
    browser.save(extraction, target)
    console.print(f"[green]✓[/green] 已写入：{target}（权限 600，这是凭据别外传）")

    async with BiliClient(cookies=extraction.mapping, retries=options.retries) as client:
        nav = await client.probe_login()

    if nav is None:
        raise NetworkError(
            f"Cookie 已存到 {target}，但没能连上 B站接口验证",
            hint="网络恢复后跑 bili-claw --check 确认；文件已经在那儿了，不用重新取",
        )
    if not nav.isLogin:
        raise AuthError(
            f"取到 {len(extraction.cookies)} 个字段并已保存，但 B站不认为这是登录态",
            hint=(
                "那个浏览器里多半只是没登录 bilibili.com（或者登录态已经过期）。"
                "确认浏览器打开 bilibili.com 时右上角是自己的头像，再跑一次 --login"
            ),
        )

    tier = "大会员" if nav.is_vip() else "非大会员"
    ceiling = (
        "4K / 8K / HDR / 杜比（仍取决于片源是否提供）"
        if nav.is_vip()
        else "1080P / 1080P60（4K / 8K / HDR 需要大会员）"
    )
    console.print(f"[green]✓[/green] 验证通过：{nav.uname}（UID {nav.mid}，{tier}）")
    console.print(f"  [dim]→ 该账号最高可下载：{ceiling}[/dim]")
    return target


async def run(options: Options, console: Console) -> int:
    if options.login:
        await run_login(options, console)
        if not options.url:
            # 只想先把登录态准备好，不下载任何东西
            return ExitCode.OK

    request, preference, rate = _validate(options)

    if options.check:
        return await run_check(options, console)

    cookies, cookie_source = resolve_cookie(explicit=options.cookie, cookie_file=options.cookie_file)
    logged_in = is_logged_in(cookies)

    downloader_limit = rate
    async with BiliClient(cookies=cookies, retries=options.retries) as client:
        if cookie_source is not None:
            await _reject_expired_cookie(client, cookie_source)
        target = await resolve_target(client, options.url)
        info = await client.view(bvid=target.bvid, aid=target.aid)
        pages, _ = select_pages(info, options.page, target.page)

        template = options.template
        if len(pages) > 1 and "p" not in template_fields(template):
            # 多P 时不加序号会互相覆盖，这里补一个并说明，而不是静默覆盖
            template = f"{template}_P{{p:02d}}"
            console.print(f"[dim]ℹ 多分P 下载，文件名模板自动调整为：{template}[/dim]")

        print_video_info(console, info, logged_in=logged_in, cookie_source=cookie_source, pages=pages)

        qn = quality.qn_for_request(request)
        if request.kind == "ask":
            console.print("[dim]ℹ 清晰度：交互选择——将列出当前账号实际可下载的档位[/dim]")
        else:
            console.print(f"[dim]ℹ 请求清晰度：{request.describe()}（qn={qn}）[/dim]")

        if options.list_qualities or options.info_only:
            data = await _playurl(client, info.bvid, pages[0].cid, qn)
            if options.list_qualities:
                print_qualities(
                    console, data, quality.to_video_streams(data),
                    preference=preference, logged_in=logged_in,
                )
            return ExitCode.OK

        downloader = StreamDownloader(
            client.http,
            concurrency=options.concurrency,
            retries=options.retries,
            limit_rate=downloader_limit,
        )

        succeeded: list[Path] = []
        failures: list[tuple[Page, BiliError]] = []
        progress = ProgressReporter(console, enabled=not options.quiet)

        for page in pages:
            console.print(f"\n[bold]▶ P{page.page}[/bold] {page.part or info.title}")
            try:
                result = await process_page(
                    client,
                    downloader,
                    info=info,
                    page=page,
                    options=options,
                    request=request,
                    preference=preference,
                    template=template,
                    logged_in=logged_in,
                    progress=progress,
                    console=console,
                )
            except BiliError as exc:
                failures.append((page, exc))
                err_console.print(f"  [red]✗ {exc.message}[/red]")
                if exc.hint:
                    for line in exc.hint.splitlines():
                        err_console.print(f"    [dim]{line}[/dim]")
                continue
            if result is not None:
                succeeded.append(result)

    if failures and succeeded:
        console.print(f"\n[yellow]🎰 抓到 {len(succeeded)} 个，失败 {len(failures)} 个[/yellow]")
        # 未完成的那几个还得再跑一次，给条能直接粘贴的命令（FR-11.5）
        console.print(f"[dim]→ 重跑：{reproduce_command()}[/dim]")
        return ExitCode.PARTIAL_FAILURE
    if failures:
        # 每个分P 刚刚已经各自报过一次错，这里不再把第一条重打一遍——同一个错误
        # 连着出现两次，看起来像两个互不相干的问题。只补上重跑命令并交出退出码。
        first = failures[0][1]
        if first.exit_code != ExitCode.USAGE:
            # 参数错了就再跑一遍同一个命令，只会得到同一个错误
            console.print(f"[dim]→ 重跑：{reproduce_command()}[/dim]")
        return first.exit_code
    return ExitCode.OK


def configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=err_console, show_path=False, show_time=False)] if debug else [],
        force=True,
    )


# --------------------------------------------------------------------------- #
# Typer 命令
# --------------------------------------------------------------------------- #


@app.command()
def cli(
    url: str = typer.Argument(None, help="视频链接、BV 号、av 号，或 b23.tv 短链"),
    quality_: str = typer.Option(
        "max", "-q", "--quality", help="清晰度：max / ask / 1080 / 720 / 80 / 116 …"
    ),
    on_unavailable: str = typer.Option(
        "degrade", "--on-unavailable", help="档位不可用时：degrade（降级+提示）或 fail（报错退出）"
    ),
    codec: str = typer.Option(None, "--codec", help="编码偏好，如 avc,hevc"),
    codec_compat: bool = typer.Option(False, "--codec-compat", help="优先 AVC(H.264)，兼容性最好"),
    audio_quality: str = typer.Option("auto", "--audio-quality", help="音轨：auto / best / 30280 …"),
    audio_only: bool = typer.Option(
        False,
        "--audio-only",
        help="只下载音频，不转码（AAC 存为 .m4a，Hi-Res 无损存为 .flac）",
    ),
    container: str = typer.Option("mp4", "--container", help="容器：mp4 或 mkv"),
    output: Path = typer.Option(Path("downloads"), "-o", "--output", help="输出根目录"),
    template: str = typer.Option(
        DEFAULT_TEMPLATE,
        "--template",
        help=f"命名模板，可用变量：{', '.join(TEMPLATE_FIELDS)}",
    ),
    page: str = typer.Option(None, "-p", "--page", help="分P：单个序号、1-3、1,3,5 或 all"),
    cookie: str = typer.Option(None, "--cookie", help="直接给出 Cookie 字符串"),
    cookie_file: Path = typer.Option(None, "--cookie-file", help="Cookie 文件（Netscape 或粘贴的一行）"),
    concurrency: int = typer.Option(DEFAULT_CONCURRENCY, "--concurrency", help="单文件并发分片数"),
    limit_rate: str = typer.Option(None, "--limit-rate", help="全局限速，如 1M / 500K"),
    retries: int = typer.Option(3, "--retries", help="网络重试次数"),
    list_qualities: bool = typer.Option(False, "--list-qualities", help="只列出可用清晰度，不下载"),
    info_only: bool = typer.Option(False, "--info", help="只显示视频信息，不下载"),
    check: bool = typer.Option(False, "--check", help="检查运行环境与登录态后退出"),
    login: bool = typer.Option(
        False, "--login", help="从本机浏览器取 B站 Cookie 存下来并验证，可单独使用"
    ),
    browser_: str = typer.Option(
        "auto", "--browser", help="配合 --login：auto / chrome / firefox / edge / brave …"
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已存在的文件（默认跳过）"),
    keep_parts: bool = typer.Option(False, "--keep-parts", help="合并后保留 .m4s 分片"),
    no_merge: bool = typer.Option(False, "--no-merge", help="只下载分片，不合并"),
    quiet: bool = typer.Option(False, "-Q", "--quiet", help="不显示进度条"),
    debug: bool = typer.Option(False, "--debug", help="输出调试日志（会自动脱敏 Cookie）"),
    version: bool = typer.Option(False, "--version", help="显示版本后退出"),
    selftest: bool = typer.Option(
        False,
        "--selftest",
        hidden=True,
        help="只跑内置合并自检，以其结果作为退出码（构建脚本冒烟测试用）",
    ),
) -> None:
    if version:
        typer.echo(f"bili-claw {__version__}（哔哩娃娃机 / BiliClaw）")
        raise typer.Exit()

    if selftest:
        # **输出刻意全是 ASCII。** 这条路径是给构建脚本判读的，它要的是"结论"而不是
        # "文案"：纯 ASCII、不碰网络、退出码即结论，三个平台的判读方式完全一致。
        #
        # 早期版本靠 `grep "自检通过"` 去读 `--check` 的中文输出，Windows CI 上整个
        # job 挂在那里，而失败信息指向合并内核——内核根本没问题。根因是 Windows 的
        # cp1252 编不出中文（见 `_force_utf8_output`，那条路径后来修掉了）。
        #
        # 编码修好之后这条依然保留：它让构建脚本**不依赖任何一句可能被改写的文案**。
        # 文案怎么改都不会影响 CI。
        failure = check_merge()
        if failure is None:
            typer.echo("merge self-check: OK")
            raise typer.Exit()
        typer.echo(f"merge self-check: FAILED: {failure}", err=True)
        raise typer.Exit(int(ExitCode.MISSING_DEPENDENCY))

    if not url and not check and not login:
        err_console.print("[red]请提供视频链接，或使用 --check / --login[/red]")
        raise typer.Exit(int(ExitCode.USAGE))

    configure_logging(debug)

    options = Options(
        url=url or "",
        quality=quality_,
        on_unavailable=on_unavailable,
        codec=codec,
        codec_compat=codec_compat,
        audio_quality=audio_quality,
        audio_only=audio_only,
        container=container,
        output=output,
        template=template,
        page=page,
        cookie=cookie,
        cookie_file=cookie_file,
        concurrency=concurrency,
        limit_rate=limit_rate,
        retries=retries,
        list_qualities=list_qualities,
        info_only=info_only,
        check=check,
        login=login,
        browser=browser_,
        overwrite=overwrite,
        keep_parts=keep_parts,
        no_merge=no_merge,
        quiet=quiet,
        debug=debug,
    )

    code = asyncio.run(run(options, Console()))
    if code:
        raise typer.Exit(int(code))


def reproduce_command() -> str:
    """把这次调用的参数原样拼回一条可粘贴的命令（FR-11.5）。

    直接读 ``sys.argv``，而不是从解析后的 options 反向拼装：反向拼装会漏掉所有
    "没写就等于默认值"的选项，拼出来的是看起来一样、语义却可能不同的另一条命令。

    整条命令要过一遍 :func:`redact`——``--cookie`` 的值就在 argv 里，原样回显会把
    凭据写进终端回滚缓冲，以及任何重定向过去的日志文件。
    """
    return redact(" ".join(["bili-claw", *(shlex.quote(part) for part in sys.argv[1:])]))


def _force_utf8_output() -> None:
    """把 stdout/stderr 钉成 UTF-8。Windows 上这一步是必需的。

    Windows 的控制台**与重定向流**默认使用 ANSI 代码页（CI 那台 runner 上是 cp1252）。
    冻结成单文件之后没有任何环境变量兜底，于是 ``--version`` 里这句

        typer.echo(f"bili-claw {__version__}（哔哩娃娃机 / BiliClaw）")

    会直接抛：

        UnicodeEncodeError: 'charmap' codec can't encode characters in position 15-20
        [PYI-7568:ERROR] Failed to execute script 'entry' due to unhandled exception!

    注意是**崩溃**而不是输出乱码——管道/重定向时 Python 用
    ``locale.getpreferredencoding()`` 决定编码，正好是 cp1252。

    钉成 UTF-8 之后两个场景都正确：真控制台上 Python 走 ``WriteConsoleW``，这个编码
    只用于内存里的转换，显示照旧；重定向时输出 UTF-8 字节，这也是现代工具的通行做法。

    ``errors="replace"`` 是兜底：任何编不出来的字符退化成 ``?``，而不是让整条命令崩掉。
    整段又套了一层 try——测试替身（比如 CliRunner 用的流）不一定有 ``reconfigure``，
    而"设置输出编码"这件事永远不该成为程序起不来的原因。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError, AttributeError):
            pass


def main() -> None:
    """控制台脚本入口：把领域异常翻译成用户提示与退出码（FR-11 / FR-12）。"""
    # 必须赶在任何输出之前——`--version` 与 `--help` 是 typer 的 eager 回调，
    # 会在下面 app() 里解析参数时就打印。
    _force_utf8_output()
    try:
        app()
    except BiliError as exc:
        # 用 Padding 给整块加左缩进：rich 自动折行时，续行才不会顶到第 0 列、
        # 看起来像另一条独立的消息。
        err_console.print(Padding(f"[bold red]✗[/bold red] {exc.message}", (0, 0, 0, 2)))
        if exc.hint:
            for line in exc.hint.splitlines():
                err_console.print(Padding(f"[dim]→ {line}[/dim]", (0, 0, 0, 2)))
        # 参数类错误不给重跑命令：照着原样再跑一遍只会得到同一个错误，
        # 用户要做的是改参数，不是重试。
        if exc.exit_code != ExitCode.USAGE:
            err_console.print(Padding(f"[dim]→ 重跑：{reproduce_command()}[/dim]", (0, 0, 0, 2)))
        raise SystemExit(int(exc.exit_code)) from None
    except KeyboardInterrupt:
        err_console.print(
            "\n[yellow]已中断。[/yellow]已下载的分片保存在 .part 文件中，"
            "重新运行同一命令会从断点继续。"
        )
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
