"""ffmpeg 调用：无损合并与产物校验（FR-05）。

合并原则是 ``-c copy``——音频和视频都是原样搬运，不做任何转码，
因此速度接近磁盘拷贝，画质与音质与源流完全一致。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from .errors import BiliError, MissingDependencyError

log = logging.getLogger("biliclaw.muxer")

#: 除了 PATH，常见的手动安装位置也找一遍——GUI 启动的进程常常没有完整 PATH。
_FFMPEG_CANDIDATES = (
    "ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)
_FFPROBE_CANDIDATES = (
    "ffprobe",
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
    "/usr/bin/ffprobe",
)

INSTALL_HINT = (
    "macOS 用 brew install ffmpeg；"
    "Ubuntu/Debian 用 sudo apt install ffmpeg；"
    "Windows 用 winget install Gyan.FFmpeg"
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


def find_ffprobe() -> str | None:
    return _find(_FFPROBE_CANDIDATES)


def require_ffmpeg() -> str:
    path = find_ffmpeg()
    if not path:
        raise MissingDependencyError(
            "未找到 ffmpeg，无法合并音视频",
            hint=f"安装后再试：{INSTALL_HINT}",
        )
    return path


async def _run_capture(command: list[str]) -> tuple[int, str, str]:
    """跑一个命令，返回 ``(退出码, stdout, stderr)``。

    两个都要收：**ffprobe 把 JSON 写到 stdout**，而 ffmpeg 把诊断写到 stderr。
    只收 stderr 的话 ffprobe 的结果会被整个丢掉——``duration_of`` 就曾因此永远
    返回 ``None``（空字符串解析 JSON 失败，被 except 吞掉，看起来只是"读不到时长"）。
    """
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


async def _run(command: list[str]) -> tuple[int, str]:
    """跑一个命令，只关心 stderr——ffmpeg 的错误信息在那里。"""
    code, _, stderr = await _run_capture(command)
    return code, stderr


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
    """拼出 ffmpeg 命令行。抽出来是为了能在测试里直接断言。"""
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

    if container == "mp4":
        # faststart 把 moov 挪到文件头，边下边播/拖动进度条才正常。
        command += ["-movflags", "+faststart"]

    command.append(str(dest))
    return command


async def merge(
    *,
    video: Path | None,
    audio: Path | None,
    dest: Path,
    container: str = "mp4",
    video_codec: str | None = None,
) -> Path:
    """把已下载的流合并成 ``dest``。任一路为空时只封装另一路。"""
    if video is None and audio is None:
        raise BiliError("没有可合并的流")

    ffmpeg = require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = build_merge_command(
        ffmpeg, video=video, audio=audio, dest=dest, container=container, video_codec=video_codec
    )

    log.debug("执行：%s", " ".join(command))
    code, stderr = await _run(command)

    if code != 0 or not dest.exists() or dest.stat().st_size == 0:
        # 失败时不留半个产物
        dest.unlink(missing_ok=True)
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else f"退出码 {code}"
        raise BiliError(
            "ffmpeg 合并失败",
            hint=f"{detail}\n  可加 --keep-parts 保留分片以便排查，或用 --no-merge 只下载不合并",
        )

    return dest


async def duration_of(path: Path) -> float | None:
    """用 ffprobe 读取时长（秒）；ffprobe 缺失或失败时返回 ``None``。"""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None

    code, stdout, _ = await _run_capture(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if code != 0:
        return None
    try:
        return float(json.loads(stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


#: 合并后校验允许的时长偏差（秒）。B站给出的稿件时长按秒取整，与流里的真实时长总有
#: 出入，超出这个幅度才值得让用户去播放确认。
DURATION_TOLERANCE = 1.5


async def probe_streams(path: Path) -> tuple[set[str], float | None] | None:
    """用 ffprobe 读出产物里实际有哪些**类型的流**，以及时长。

    探测不出来（ffprobe 缺失或执行失败）时返回 ``None``，调用方据此**放弃校验**。
    这一点是刻意的：读不出来不等于文件坏，不能因为探测失败就否定一个已经合并好的
    产物，更不能顺手把它删掉。
    """
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None

    code, stdout, _ = await _run_capture(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if code != 0:
        return None
    try:
        payload = json.loads(stdout)
        kinds = {item["codec_type"] for item in payload.get("streams", []) if item.get("codec_type")}
        raw = payload.get("format", {}).get("duration")
        duration = float(raw) if raw is not None else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return kinds, duration


async def verify_output(
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
    probed = await probe_streams(path)
    if probed is None:
        return []

    kinds, duration = probed
    wanted = (("video", "视频", expect_video), ("audio", "音频", expect_audio))
    missing = [label for kind, label, want in wanted if want and kind not in kinds]
    if missing:
        raise BiliError(
            f"合并后的文件缺少{'与'.join(missing)}流：{path}",
            hint="ffmpeg 报告成功但产物不完整，多半是某条流下载时就坏了。\n"
            "  分片已保留，加 --keep-parts 重跑可对照，或手动合并排查",
        )

    if expected_duration and duration and abs(duration - expected_duration) > DURATION_TOLERANCE:
        return [
            f"产物时长 {duration:.1f}s 与稿件标称的 {expected_duration:.0f}s 相差较大，"
            "建议播放确认一下（稿件时长也可能本身是按秒取整的）"
        ]
    return []
