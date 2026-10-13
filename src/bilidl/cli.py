"""命令行入口。

对应需求：FR-01 输入解析、FR-02 信息展示、FR-03 清晰度选择、FR-04 下载、
FR-05 合并、FR-07 命名、FR-09 登录态、FR-11 进度与退出码、FR-12 错误提示。

用法示例::

    bilidl "https://www.bilibili.com/video/BV1xx411c7mD"
    bilidl BV1xx411c7mD -q 720
    bilidl <URL> -q 1080 --cookie-file ~/bili-cookies.txt
    bilidl <URL> --list-qualities
"""

from __future__ import annotations

import asyncio
import logging
import re
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

from . import __version__, quality
from .api import BiliClient
from .cookies import is_logged_in, resolve_cookie
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
from .muxer import choose_container, find_ffmpeg, merge
from .naming import TEMPLATE_FIELDS, render_path, template_fields, validate_template
from .progress import ProgressReporter

app = typer.Typer(
    add_completion=False,
    help="B站视频下载器——支持清晰度选择、Cookie 登录下载 1080P+、DASH 并发下载与 ffmpeg 无损合并。",
)

err_console = Console(stderr=True)

BVID_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})")
AID_PATTERN = re.compile(r"(?<![A-Za-z])av(\d{1,12})", re.IGNORECASE)
SHORT_LINK_PATTERN = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)
PAGE_QUERY_PATTERN = re.compile(r"[?&]p=(\d+)")

DEFAULT_TEMPLATE = "{up}/{title}"


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
        logging.getLogger("bilidl").debug("playurl 返回权限错误，刷新 WBI 后重试")
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
        logging.getLogger("bilidl").info("%s 的下载链接已过期，正在自动换链", label)
        fresh_urls, fresh_size = await refresh()
        return await downloader.download(
            fresh_urls, dest, on_progress=on_progress, on_total=on_total, total_hint=fresh_size
        )


def build_fields(info: VideoInfo, page: Page, *, qn: int, label: str) -> dict[str, object]:
    published = info.pubdate or 0
    return {
        "title": info.title,
        "bvid": info.bvid,
        "aid": info.aid,
        "p": page.page,
        "p_name": page.part,
        "up": info.owner.name,
        "mid": info.owner.mid,
        "qn": qn,
        "quality": label,
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

    output = await merge(
        video=video_temp,
        audio=audio_temp if audio is not None else None,
        dest=dest,
        container=container,
        video_codec=stream.codec if stream else None,
    )

    if not options.keep_parts:
        video_temp.unlink(missing_ok=True)
        audio_temp.unlink(missing_ok=True)

    size_mb = output.stat().st_size / 1024 / 1024
    console.print(f"  [green]✓[/green] {output}　[dim]({size_mb:.1f} MB)[/dim]")
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
    """``--check``：确认依赖与登录态，便于安装后第一时间发现问题（对应 E2E-9）。

    返回最具体的那一个退出码（FR-11），而不是笼统的失败码——脚本据此就能判断
    是该装 ffmpeg、该查网络，还是该重新登录。
    """
    code = ExitCode.OK

    def failed(candidate: ExitCode) -> None:
        """只让第一个失败原因占据退出码。

        三项检查是依次做的，后面发现问题时不该把前面的结论冲掉：没有 ffmpeg 又刚好
        断网时，真正该先解决的是装 ffmpeg（退出码 6），而不是让人去排查网络。
        顺序本身编码了优先级：依赖 → 网络/登录态。
        """
        nonlocal code
        if code is ExitCode.OK:
            code = candidate

    ffmpeg = find_ffmpeg()
    if ffmpeg:
        console.print(f"[green]✓[/green] ffmpeg：{ffmpeg}")
    else:
        failed(ExitCode.MISSING_DEPENDENCY)
        console.print("[red]✗[/red] 未找到 ffmpeg——音视频合并无法进行（退出码 6）")
        console.print("  [dim]→ macOS: brew install ffmpeg[/dim]")

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


async def run(options: Options, console: Console) -> int:
    request, preference, rate = _validate(options)

    if options.check:
        return await run_check(options, console)

    cookies, cookie_source = resolve_cookie(explicit=options.cookie, cookie_file=options.cookie_file)
    logged_in = is_logged_in(cookies)

    downloader_limit = rate
    async with BiliClient(cookies=cookies, retries=options.retries) as client:
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
                console.print(f"  [red]✗ {exc.message}[/red]")
                if exc.hint:
                    for line in exc.hint.splitlines():
                        console.print(f"    [dim]{line}[/dim]")
                continue
            if result is not None:
                succeeded.append(result)

    if failures and succeeded:
        console.print(f"\n[yellow]完成 {len(succeeded)} 个，失败 {len(failures)} 个[/yellow]")
        return ExitCode.PARTIAL_FAILURE
    if failures:
        raise failures[0][1]
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
    check: bool = typer.Option(False, "--check", help="检查 ffmpeg 与登录态后退出"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已存在的文件（默认跳过）"),
    keep_parts: bool = typer.Option(False, "--keep-parts", help="合并后保留 .m4s 分片"),
    no_merge: bool = typer.Option(False, "--no-merge", help="只下载分片，不调用 ffmpeg 合并"),
    quiet: bool = typer.Option(False, "-Q", "--quiet", help="不显示进度条"),
    debug: bool = typer.Option(False, "--debug", help="输出调试日志（会自动脱敏 Cookie）"),
    version: bool = typer.Option(False, "--version", help="显示版本后退出"),
) -> None:
    if version:
        typer.echo(f"bilidl {__version__}")
        raise typer.Exit()

    if not url and not check:
        err_console.print("[red]请提供视频链接，或使用 --check 检查环境[/red]")
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
        overwrite=overwrite,
        keep_parts=keep_parts,
        no_merge=no_merge,
        quiet=quiet,
        debug=debug,
    )

    code = asyncio.run(run(options, Console()))
    if code:
        raise typer.Exit(int(code))


def main() -> None:
    """控制台脚本入口：把领域异常翻译成用户提示与退出码（FR-11 / FR-12）。"""
    try:
        app()
    except BiliError as exc:
        # 用 Padding 给整块加左缩进：rich 自动折行时，续行才不会顶到第 0 列、
        # 看起来像另一条独立的消息。
        err_console.print(Padding(f"[bold red]✗[/bold red] {exc.message}", (0, 0, 0, 2)))
        if exc.hint:
            for line in exc.hint.splitlines():
                err_console.print(Padding(f"[dim]→ {line}[/dim]", (0, 0, 0, 2)))
        raise SystemExit(int(exc.exit_code)) from None
    except KeyboardInterrupt:
        err_console.print(
            "\n[yellow]已中断。[/yellow]已下载的分片保存在 .part 文件中，"
            "重新运行同一命令会从断点继续。"
        )
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
