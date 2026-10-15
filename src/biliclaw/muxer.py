"""合并与产物校验（FR-05）。

合并由 :mod:`biliclaw.mp4` 用**纯 Python** 完成：B站的 DASH 分片本身就是自包含的
fMP4，样本数据一个字节都不用改，重排容器结构就够了，不需要外部程序，也就没有
"先装 ffmpeg"这个前提。

**唯一的例外是 MKV**：``--container mkv``，以及 Hi-Res 无损音轨（FLAC 装进 MP4
属于非标准组合，播放器兼容性很差）会自动改走 MKV。这条路径仍然调用 ffmpeg——
MP4 的盒子结构换不来 Matroska。没装 ffmpeg 时给出的是一条能照做的提示，而不是
一个失败的下载。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from . import mp4
from .errors import BiliError, MissingDependencyError

log = logging.getLogger("biliclaw.muxer")

#: 除了 PATH，常见的手动安装位置也找一遍——GUI 启动的进程常常没有完整 PATH。
_FFMPEG_CANDIDATES = (
    "ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)

INSTALL_HINT = (
    "macOS 用 brew install ffmpeg；"
    "Ubuntu/Debian 用 sudo apt install ffmpeg；"
    "Windows 用 winget install Gyan.FFmpeg"
)

#: MKV 之外都要 ffmpeg 吗？不用——MP4 走纯 Python。这条提示只在真的需要时才出现。
MKV_HINT = (
    "只有 MKV 容器需要 ffmpeg（MP4 合并已内置，不需要任何外部程序）。\n"
    "  想省掉这一步：去掉 --container mkv，或把音轨换成 AAC（--audio-quality auto）"
)


def _find(candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if "/" in candidate:
            if Path(candidate).is_file():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def find_ffmpeg() -> str | None:
    return _find(_FFMPEG_CANDIDATES)


def require_ffmpeg() -> str:
    path = find_ffmpeg()
    if not path:
        raise MissingDependencyError("未找到 ffmpeg，无法封装 MKV", hint=MKV_HINT)
    return path


async def _run_capture(command: list[str]) -> tuple[int, str, str]:
    """跑一个命令，返回 ``(退出码, stdout, stderr)``。"""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def choose_container(
    requested: str,
    *,
    video_codec: str | None,
    audio_codec: str | None,
) -> tuple[str, str | None]:
    """决定最终容器；无法无损装进 MP4 时自动改用 MKV 并给出告警（FR-03.6）。

    返回 ``(容器, 告警文本)``。
    """
    container = (requested or "mp4").lower()

    # FLAC 无损音轨在 MP4 里属于非标准组合，播放器兼容性很差，必须走 MKV。
    if audio_codec and audio_codec.startswith("flac") and container == "mp4":
        return "mkv", "所选音轨是无损 FLAC，MP4 无法标准容纳，已自动改用 MKV 容器"

    if container not in {"mp4", "mkv"}:
        return "mp4", f"不支持的容器 {requested}，已回退为 MP4"

    return container, None


def build_merge_command(
    ffmpeg: str,
    *,
    video: Path | None,
    audio: Path | None,
    dest: Path,
    container: str,
    video_codec: str | None = None,
) -> list[str]:
    """拼出 MKV 路径的 ffmpeg 命令行。抽出来是为了能在测试里直接断言。"""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]

    if video is not None:
        command += ["-i", str(video)]
    if audio is not None:
        command += ["-i", str(audio)]

    if video is not None:
        command += ["-map", "0:v:0"]
        if audio is not None:
            command += ["-map", "1:a:0"]
    elif audio is not None:
        command += ["-map", "0:a:0"]

    command += ["-c", "copy"]

    # HEVC 默认打成 hev1，QuickTime/Apple 生态只认 hvc1；换成 hvc1 不重新编码。
    if video_codec == "hevc":
        command += ["-tag:v", "hvc1"]

    command.append(str(dest))
    return command


async def merge(
    *,
    video: Path | None,
    audio: Path | None,
    dest: Path,
    container: str = "mp4",
    video_codec: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """把已下载的流合并成 ``dest``。任一路为空时只封装另一路。

    MP4 走内置的纯 Python 合并器；MKV 才需要 ffmpeg。

    ``progress`` 只对 MP4 有意义（MKV 那一路的进度由 ffmpeg 自己管，它也快得没必要
    报），参数是 ``(已写字节, 总字节)``。
    """
    if video is None and audio is None:
        raise BiliError("没有可合并的流")

    dest.parent.mkdir(parents=True, exist_ok=True)

    if container == "mp4":
        # 合并是几百 MB 的搬运，扔到线程里做，别把事件循环占住
        return await asyncio.to_thread(mp4.merge, video=video, audio=audio, dest=dest, progress=progress)

    ffmpeg = require_ffmpeg()
    command = build_merge_command(
        ffmpeg, video=video, audio=audio, dest=dest, container=container, video_codec=video_codec
    )
    log.debug("执行：%s", " ".join(command))
    code, _, stderr = await _run_capture(command)

    if code != 0 or not dest.exists() or dest.stat().st_size == 0:
        # 失败时不留半个产物
        dest.unlink(missing_ok=True)
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else f"退出码 {code}"
        raise BiliError(
            "ffmpeg 封装 MKV 失败",
            hint=f"{detail}\n  可加 --keep-parts 保留分片以便排查，或用 --no-merge 只下载不合并",
        )

    return dest


def duration_of(path: Path) -> float | None:
    """读产物时长（秒）；读不出来时返回 ``None``。"""
    info = mp4.probe(path)
    return info.duration if info else None


#: 合并后校验允许的时长偏差（秒）。B站给出的稿件时长按秒取整，与流里的真实时长总有
#: 出入，超出这个幅度才值得让用户去播放确认。
DURATION_TOLERANCE = 1.5


def probe_streams(path: Path) -> tuple[set[str], float | None] | None:
    """读出产物里实际有哪些**类型的流**，以及时长。

    探测不出来（文件不是可识别的 MP4）时返回 ``None``，调用方据此**放弃校验**。
    这一点是刻意的：读不出来不等于文件坏，不能因为探测失败就否定一个已经合并好的
    产物，更不能顺手把它删掉。
    """
    info = mp4.probe(path)
    if info is None:
        return None
    return info.kinds, info.duration


def verify_output(
    path: Path,
    *,
    expect_video: bool,
    expect_audio: bool,
    expected_duration: float | None = None,
) -> list[str]:
    """合并后校验（FR-05.4）。返回要讲给用户的告警；致命问题直接抛错。

    抛错时产物**故意留着**：它也许还能手工修复，而调用方正是收到异常才不会继续
    往下走删掉分片（FR-05.5）。
    """
    probed = probe_streams(path)
    if probed is None:
        return []

    kinds, duration = probed
    wanted = (("video", "视频", expect_video), ("audio", "音频", expect_audio))
    missing = [label for kind, label, want in wanted if want and kind not in kinds]
    if missing:
        raise BiliError(
            f"合并后的文件缺少{'与'.join(missing)}流：{path}",
            hint="合并报告成功但产物不完整，多半是某条流下载时就坏了。\n"
            "  分片已保留，加 --keep-parts 重跑可对照排查",
        )

    if expected_duration and duration and abs(duration - expected_duration) > DURATION_TOLERANCE:
        return [
            f"产物时长 {duration:.1f}s 与稿件标称的 {expected_duration:.0f}s 相差较大，"
            "建议播放确认一下（稿件时长也可能本身是按秒取整的）"
        ]
    return []
