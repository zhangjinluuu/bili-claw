"""合并层测试。

``mp4`` 内核的结构正确性在 ``test_mp4.py`` 里验证；这里只管 ``muxer`` 这一层的
调度与校验：容器怎么选、MP4 走内置合并器还是 MKV 走 ffmpeg、产物怎么复核。

「MP4 合并不再需要 ffmpeg」是这次改动的全部意义，所以它有一条专门的回归测试
（``test_mp4_merge_never_looks_for_ffmpeg``）。
"""

from __future__ import annotations

from pathlib import Path

import fmp4
import pytest

from biliclaw import mp4, muxer
from biliclaw.errors import BiliError, MissingDependencyError
from biliclaw.muxer import (
    DURATION_TOLERANCE,
    build_merge_command,
    choose_container,
    probe_streams,
    verify_output,
)

# --------------------------------------------------------------------------- #
# 容器选择（FR-03.6）
# --------------------------------------------------------------------------- #


def test_choose_container_switches_flac_to_mkv() -> None:
    """FLAC 装进 MP4 是非标准组合，播放器兼容性很差，必须自动改 MKV 并告知。"""
    container, warning = choose_container("mp4", video_codec="avc", audio_codec="flac")
    assert container == "mkv"
    assert warning and "MKV" in warning


def test_choose_container_leaves_compatible_combinations_alone() -> None:
    assert choose_container("mp4", video_codec="avc", audio_codec="mp4a.40.2") == ("mp4", None)
    assert choose_container("mkv", video_codec="avc", audio_codec="flac") == ("mkv", None)


def test_choose_container_falls_back_on_unknown_container() -> None:
    container, warning = choose_container("webm", video_codec="avc", audio_codec="mp4a.40.2")
    assert container == "mp4"
    assert warning and "webm" in warning


# --------------------------------------------------------------------------- #
# MKV 路径的命令行拼装（唯一还需要 ffmpeg 的地方）
# --------------------------------------------------------------------------- #


def test_build_merge_command_maps_one_video_and_one_audio() -> None:
    command = build_merge_command(
        "/usr/bin/ffmpeg",
        video=Path("/tmp/v.m4s"),
        audio=Path("/tmp/a.m4s"),
        dest=Path("/tmp/out.mkv"),
        container="mkv",
    )
    # -map 0:v:0 / 1:a:0 缺一不可：少了它 ffmpeg 会自己挑流，多音轨视频上会挑错。
    assert command[command.index("-map") + 1] == "0:v:0"
    assert command[command.index("-map", command.index("-map") + 1) + 1] == "1:a:0"
    assert command[-1] == "/tmp/out.mkv"
    assert command[command.index("-c") + 1] == "copy"


def test_build_merge_command_handles_audio_only() -> None:
    command = build_merge_command(
        "/usr/bin/ffmpeg", video=None, audio=Path("/tmp/a.m4s"), dest=Path("/tmp/o.mka"), container="mkv"
    )
    assert "0:a:0" in command
    assert "0:v:0" not in command


def test_build_merge_command_tags_hevc_as_hvc1() -> None:
    """HEVC 默认打成 hev1，Apple 生态只认 hvc1；换标签不重新编码。"""
    command = build_merge_command(
        "/usr/bin/ffmpeg",
        video=Path("/tmp/v.m4s"),
        audio=None,
        dest=Path("/tmp/o.mkv"),
        container="mkv",
        video_codec="hevc",
    )
    assert command[command.index("-tag:v") + 1] == "hvc1"


def test_build_merge_command_never_passes_movflags() -> None:
    """``+faststart`` 是 MP4 的原子；这条路径只产出 MKV，塞进去会让 ffmpeg 报错。"""
    command = build_merge_command(
        "/usr/bin/ffmpeg", video=Path("/v"), audio=None, dest=Path("/o"), container="mkv"
    )
    assert not any("faststart" in item for item in command)


# --------------------------------------------------------------------------- #
# 调度：MP4 不该碰 ffmpeg，MKV 才需要
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fragments(tmp_path: Path) -> tuple[Path, Path]:
    video, audio = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(handler=b"vide", codec=b"avc1", sizes=[100, 50], durations=[512, 512]).write(video)
    fmp4.build(
        handler=b"soun", codec=b"mp4a", timescale=44100, sizes=[30], durations=[1024]
    ).write(audio)
    return video, audio


async def test_mp4_merge_never_looks_for_ffmpeg(
    fragments: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """核心回归：机器上完全没有 ffmpeg 时，MP4 合并必须照样成功。

    这正是「发布产物不用装任何依赖」的技术前提，所以用 monkeypatch 把查找结果
    钉死成 ``None``，而不是依赖跑测试的机器上恰好装了 ffmpeg。
    """
    monkeypatch.setattr(muxer, "find_ffmpeg", lambda: None)
    video, audio = fragments
    dest = tmp_path / "out.mp4"

    merged = await muxer.merge(
        video=video, audio=audio, dest=dest, container="mp4", video_codec="avc"
    )

    assert merged == dest and dest.stat().st_size > 0
    assert mp4.probe(dest) is not None


async def test_mkv_merge_says_what_to_install_when_ffmpeg_is_missing(
    fragments: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(muxer, "find_ffmpeg", lambda: None)
    video, audio = fragments
    with pytest.raises(MissingDependencyError) as excinfo:
        await muxer.merge(video=video, audio=audio, dest=tmp_path / "o.mkv", container="mkv")
    # 提示必须说清楚"只有 MKV 需要它"，否则用户会以为整个工具坏了
    assert "MKV" in (excinfo.value.hint or "")


async def test_merge_refuses_an_empty_request(tmp_path: Path) -> None:
    with pytest.raises(BiliError):
        await muxer.merge(video=None, audio=None, dest=tmp_path / "o.mp4")


# --------------------------------------------------------------------------- #
# 合并后校验（FR-05.4）
# --------------------------------------------------------------------------- #


def test_probe_streams_reads_kinds_and_duration(fragments: tuple[Path, Path], tmp_path: Path) -> None:
    video, audio = fragments
    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    probed = probe_streams(dest)
    assert probed is not None
    kinds, duration = probed
    assert kinds == {"video", "audio"}
    assert duration is not None and duration > 0


def test_probe_streams_gives_up_on_something_it_cannot_read(tmp_path: Path) -> None:
    """读不出来 ≠ 文件坏。放弃校验，而不是否定一个已经合并好的产物。"""
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"definitely not an mp4")
    assert probe_streams(junk) is None
    assert probe_streams(tmp_path / "missing.mp4") is None


def test_duration_of_reads_the_movie_header(fragments: tuple[Path, Path], tmp_path: Path) -> None:
    video, audio = fragments
    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    duration = muxer.duration_of(dest)
    assert duration is not None and duration > 0
    assert muxer.duration_of(tmp_path / "missing.mp4") is None


def test_verify_output_raises_when_a_stream_is_missing(fragments: tuple[Path, Path], tmp_path: Path) -> None:
    """这是 FR-05.4 真正要拦的情况：合并报告成功，但产物里根本没有音频。"""
    video, _ = fragments
    dest = mp4.merge(video=video, audio=None, dest=tmp_path / "out.mp4")

    with pytest.raises(BiliError) as excinfo:
        verify_output(dest, expect_video=True, expect_audio=True, expected_duration=1.0)
    assert "音频" in excinfo.value.message
    # 分片要留着给用户排查，所以提示里得说明这件事
    assert "分片已保留" in (excinfo.value.hint or "")


def test_verify_output_accepts_a_complete_file(fragments: tuple[Path, Path], tmp_path: Path) -> None:
    video, audio = fragments
    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    assert verify_output(
        dest, expect_video=True, expect_audio=True, expected_duration=muxer.duration_of(dest)
    ) == []


def test_verify_output_warns_on_a_large_duration_gap(fragments: tuple[Path, Path], tmp_path: Path) -> None:
    video, audio = fragments
    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    warnings = verify_output(
        dest, expect_video=True, expect_audio=True, expected_duration=1800.0
    )
    assert len(warnings) == 1
    assert "时长" in warnings[0]


def test_verify_output_tolerates_rounding(fragments: tuple[Path, Path], tmp_path: Path) -> None:
    """稿件时长按秒取整，差一点点是常态，不该每次都惊动用户。"""
    video, audio = fragments
    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    actual = muxer.duration_of(dest)
    assert actual is not None
    assert verify_output(
        dest,
        expect_video=True,
        expect_audio=True,
        expected_duration=actual + DURATION_TOLERANCE / 2,
    ) == []


def test_verify_output_skips_checks_it_cannot_perform(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not an mp4")
    assert verify_output(junk, expect_video=True, expect_audio=True, expected_duration=1.0) == []


def test_verify_output_only_requires_the_streams_that_were_merged(
    fragments: tuple[Path, Path], tmp_path: Path
) -> None:
    """纯视频流（稿件本来就无音轨）不该被判成缺音频。"""
    video, _ = fragments
    dest = mp4.merge(video=video, audio=None, dest=tmp_path / "out.mp4")
    assert verify_output(
        dest, expect_video=True, expect_audio=False, expected_duration=muxer.duration_of(dest)
    ) == []


async def test_mp4_merge_forwards_the_progress_callback(tmp_path: Path, monkeypatch) -> None:
    """CLI 把进度回调交给 ``muxer.merge``，它必须一路传到 ``mp4.merge``。

    这条是补课：改 ``mp4.merge`` 签名时漏了 ``muxer.merge`` 的中转，单测各自都是绿的，
    直到真的敲一次 ``bclaw`` 才炸出 ``TypeError: merge() got an unexpected keyword
    argument 'progress'``。跨层参数就得有一条跨层的用例守着。
    """
    seen: list[tuple[int, int]] = []
    real = muxer.mp4.merge

    def spy(**kwargs):
        kwargs["progress"](10, 20)
        kwargs["progress"] = None
        return real(**kwargs)

    monkeypatch.setattr(muxer.mp4, "merge", spy)

    video = tmp_path / "v.m4s"
    audio = tmp_path / "a.m4s"
    fmp4.build(seed=1).write(video)
    fmp4.build(handler=b"soun", codec=b"mp4a", timescale=44100, sizes=[40], durations=[1024], seed=2).write(audio)

    await muxer.merge(video=video, audio=audio, dest=tmp_path / "out.mp4", progress=lambda d, t: seen.append((d, t)))
    assert seen == [(10, 20)]
