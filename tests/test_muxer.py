"""合并层测试。

这一层此前完全没有测试——审查时只被读过、从未被执行过，而它决定了产物能不能播。
FR-05.4 的合并后校验就是在这个盲区里漏掉的。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from biliclaw import muxer
from biliclaw.errors import BiliError
from biliclaw.muxer import (
    DURATION_TOLERANCE,
    build_merge_command,
    choose_container,
    find_ffmpeg,
    probe_streams,
    verify_output,
)

# --------------------------------------------------------------------------- #
# 命令行拼装
# --------------------------------------------------------------------------- #


def test_build_merge_command_maps_one_video_and_one_audio() -> None:
    command = build_merge_command(
        "/usr/bin/ffmpeg",
        video=Path("/tmp/v.m4s"),
        audio=Path("/tmp/a.m4s"),
        dest=Path("/tmp/out.mp4"),
        container="mp4",
    )
    # -map 0:v:0 / 1:a:0 缺一不可：少了它 ffmpeg 会自己挑流，多音轨视频上会挑错。
    assert command[command.index("-map") + 1] == "0:v:0"
    assert command[command.index("-map", command.index("-map") + 1) + 1] == "1:a:0"
    assert command[-1] == "/tmp/out.mp4"
    assert "-c" in command and command[command.index("-c") + 1] == "copy"


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
        dest=Path("/tmp/o.mp4"),
        container="mp4",
        video_codec="hevc",
    )
    assert command[command.index("-tag:v") + 1] == "hvc1"


def test_build_merge_command_faststart_only_for_mp4() -> None:
    """faststart 是 MP4 的原子；塞给 MKV 会让 ffmpeg 报错。"""
    mp4 = build_merge_command(
        "/usr/bin/ffmpeg", video=Path("/v"), audio=None, dest=Path("/o"), container="mp4"
    )
    mkv = build_merge_command(
        "/usr/bin/ffmpeg", video=Path("/v"), audio=None, dest=Path("/o"), container="mkv"
    )
    assert "+faststart" in mp4
    assert "+faststart" not in mkv


# --------------------------------------------------------------------------- #
# 容器选择（FR-05.6）
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
# 合并后校验（FR-05.4）
# --------------------------------------------------------------------------- #


def _payload(kinds: list[str], duration: float | None) -> str:
    return json.dumps(
        {
            "streams": [{"codec_type": kind} for kind in kinds],
            "format": {} if duration is None else {"duration": str(duration)},
        }
    )


def _stub_probe(monkeypatch: pytest.MonkeyPatch, *, code: int = 0, output: str = "", present: bool = True) -> None:
    monkeypatch.setattr(muxer, "find_ffprobe", lambda: "/usr/bin/ffprobe" if present else None)

    async def _run_capture(command: list[str]) -> tuple[int, str, str]:
        # ffprobe 的 JSON 走 stdout；stderr 留空，正是真实成功调用的样子
        return code, output, ""

    monkeypatch.setattr(muxer, "_run_capture", _run_capture)


async def test_duration_of_reads_stdout_not_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：``_run`` 只收 stderr，而 ffprobe 把 JSON 写到 stdout。

    两者共用同一个 runner 时，``duration_of`` 拿到的永远是空字符串，解析失败被
    ``except`` 吞掉，表现成"读不到时长"——一个从不报错、只是永远不工作的函数。
    """
    _stub_probe(monkeypatch, output=_payload(["video"], 116.54))
    assert await muxer.duration_of(Path("/tmp/out.mp4")) == 116.54


async def test_probe_streams_reads_kinds_and_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(monkeypatch, output=_payload(["video", "audio"], 116.54))
    assert await probe_streams(Path("/tmp/out.mp4")) == ({"video", "audio"}, 116.54)


async def test_probe_streams_gives_up_when_ffprobe_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(monkeypatch, present=False)
    assert await probe_streams(Path("/tmp/out.mp4")) is None


async def test_probe_streams_gives_up_on_failure_or_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(monkeypatch, code=1)
    assert await probe_streams(Path("/tmp/out.mp4")) is None

    _stub_probe(monkeypatch, output="not json")
    assert await probe_streams(Path("/tmp/out.mp4")) is None


async def test_verify_output_raises_when_a_stream_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """这是 FR-05.4 真正要拦的情况：ffmpeg 退出码为 0，但产物里根本没有音频。"""
    _stub_probe(monkeypatch, output=_payload(["video"], 100.0))
    with pytest.raises(BiliError) as excinfo:
        await verify_output(
            Path("/tmp/out.mp4"), expect_video=True, expect_audio=True, expected_duration=100.0
        )
    assert "音频" in excinfo.value.message
    # 分片要留着给用户排查，所以提示里得说明这件事
    assert "分片已保留" in (excinfo.value.hint or "")


async def test_verify_output_accepts_a_complete_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(monkeypatch, output=_payload(["video", "audio"], 116.54))
    assert await verify_output(
        Path("/tmp/out.mp4"), expect_video=True, expect_audio=True, expected_duration=117.0
    ) == []


async def test_verify_output_warns_on_a_large_duration_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(monkeypatch, output=_payload(["video", "audio"], 60.0))
    warnings = await verify_output(
        Path("/tmp/out.mp4"), expect_video=True, expect_audio=True, expected_duration=1800.0
    )
    assert len(warnings) == 1
    assert "时长" in warnings[0]


async def test_verify_output_tolerates_rounding(monkeypatch: pytest.MonkeyPatch) -> None:
    """稿件时长按秒取整，差一点点是常态，不该每次都惊动用户。"""
    _stub_probe(monkeypatch, output=_payload(["video", "audio"], 116.54))
    assert await verify_output(
        Path("/tmp/out.mp4"),
        expect_video=True,
        expect_audio=True,
        expected_duration=116.54 + DURATION_TOLERANCE / 2,
    ) == []


async def test_verify_output_skips_checks_it_cannot_perform(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测不出来就放弃校验——不能因为读不出来就否定一个已经合并好的文件。"""
    _stub_probe(monkeypatch, present=False)
    assert await verify_output(
        Path("/tmp/out.mp4"), expect_video=True, expect_audio=True, expected_duration=1.0
    ) == []


async def test_verify_output_only_requires_the_streams_that_were_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯视频流（稿件本来就无音轨）不该被判成缺音频。"""
    _stub_probe(monkeypatch, output=_payload(["video"], 30.0))
    assert await verify_output(
        Path("/tmp/out.mp4"), expect_video=True, expect_audio=False, expected_duration=30.0
    ) == []


# --------------------------------------------------------------------------- #
# 真实 ffprobe 集成：上面的桩证明了逻辑，这里证明我们读得懂 ffprobe 的实际输出
# --------------------------------------------------------------------------- #


def _make_clip(target: Path, *, with_audio: bool) -> None:
    ffmpeg = find_ffmpeg() or "ffmpeg"
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5",
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100", "-shortest"]
    command += ["-c:v", "libx264"]
    if with_audio:
        command += ["-c:a", "aac"]
    else:
        command += ["-an"]
    command.append(str(target))
    subprocess.run(command, check=True, capture_output=True)


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """``(带音轨的片段, 无音轨的片段)``。

    片段在 fixture 里生成而不是在测试体里：测试是 async 的，在里面跑 `subprocess.run`
    会阻塞事件循环（ruff 的 ASYNC221 说的就是这件事）。
    """
    if not find_ffmpeg():
        pytest.skip("需要真实的 ffmpeg/ffprobe")
    directory = tmp_path_factory.mktemp("clips")
    both, silent = directory / "both.mp4", directory / "silent.mp4"
    _make_clip(both, with_audio=True)
    _make_clip(silent, with_audio=False)
    return both, silent


async def test_probe_streams_against_a_real_file(clips: tuple[Path, Path]) -> None:
    """上面的桩证明了逻辑，这里证明我们读得懂 ffprobe 的实际输出。"""
    target, silent = clips

    probed = await probe_streams(target)
    assert probed is not None
    kinds, duration = probed
    assert kinds == {"video", "audio"}
    assert duration is not None and 0.5 < duration < 2.0

    # 真的合并层校验：完整文件应当毫无异议
    assert await verify_output(
        target, expect_video=True, expect_audio=True, expected_duration=duration
    ) == []

    # 时长差得离谱时只告警，不拦——产物还是能播的
    assert len(
        await verify_output(target, expect_video=True, expect_audio=True, expected_duration=duration * 100)
    ) == 1

    # 而要求一条根本不存在的流时，必须拦住：这正是"ffmpeg 报成功但产物残缺"
    with pytest.raises(BiliError):
        await verify_output(silent, expect_video=True, expect_audio=True, expected_duration=1.0)

    # 只合并了一条流时，就别去要求另一条
    assert await verify_output(
        silent, expect_video=True, expect_audio=False, expected_duration=1.0
    ) == []
