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

log = logging.getLogger("bilidl.muxer")

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


async def _run(command: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    return process.returncode or 0, stderr.decode("utf-8", errors="replace")


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

    code, output = await _run(
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
        return float(json.loads(output)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None
