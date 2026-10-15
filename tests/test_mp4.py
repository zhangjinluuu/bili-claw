"""``biliclaw.mp4`` 的测试：纯 Python 合并内核。

这里的断言分两层：

* **读回来的数据对不对**——用一个**独立实现**的样本表读取器（本文件里的
  ``track_bytes``）从产物里把每条轨的样本数据捞回来，与源分片的原始字节比对。
  刻意不复用 ``mp4`` 自己的解析代码，否则就是拿实现验证实现。
* **表本身写对没有**——块偏移、样本数、轨道编号、时长这些字段逐项检查。

能播不能播由真机测试负责（见文件末尾），这里只保证结构正确。
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import fmp4
import pytest

from biliclaw import mp4
from biliclaw.errors import BiliError

# --------------------------------------------------------------------------- #
# 独立的样本表读取器
# --------------------------------------------------------------------------- #


def _iter(blob: bytes, start: int, end: int):
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(blob[offset : offset + 4], "big")
        kind = blob[offset + 4 : offset + 8]
        if size == 1:
            size = int.from_bytes(blob[offset + 8 : offset + 16], "big")
        elif size == 0:
            size = end - offset
        yield offset, size, kind
        offset += size


def _payload(blob: bytes, path: list[bytes], where: tuple[int, int] | None = None) -> tuple[int, int]:
    start, end = where or (0, len(blob))
    for name in path:
        for offset, size, kind in _iter(blob, start, end):
            if kind == name:
                start, end = offset + 8, offset + size
                break
        else:
            raise AssertionError(f"产物里缺少 {name!r}")
    return start, end


def _tracks(blob: bytes) -> list[tuple[int, int]]:
    """每条 ``trak`` 的**内部**范围 ``(起点, 终点)``。

    传的是子盒区域而不是盒子本身的 ``(偏移, 长度)``：``_iter`` 从给定位置开始遍历同级
    盒子，若从 ``trak`` 自己的起点开始，第一个看到的永远是 ``trak`` 本身。
    """
    moov = _payload(blob, [b"moov"])
    return [(offset + 8, offset + size) for offset, size, kind in _iter(blob, *moov) if kind == b"trak"]


def _box(blob: bytes, path: list[bytes], where=None) -> bytes:
    start, end = _payload(blob, path, where)
    return blob[start - 8 : end]


def track_sample_bytes(path: Path, index: int) -> bytes:
    """按产物自己的样本表，把第 ``index`` 条轨的样本数据原样读回来。"""
    blob = path.read_bytes()
    trak = _tracks(blob)[index]

    stsz = _box(blob, [b"mdia", b"minf", b"stbl", b"stsz"], trak)
    uniform, count = struct.unpack_from(">II", stsz, 12)
    sizes = [uniform] * count if uniform else list(struct.unpack_from(f">{count}I", stsz, 20))

    stsc = _box(blob, [b"mdia", b"minf", b"stbl", b"stsc"], trak)
    runs = struct.unpack_from(">I", stsc, 12)[0]
    entries = [
        struct.unpack_from(">III", stsc, 16 + 12 * i) for i in range(runs)
    ]

    try:
        stco = _box(blob, [b"mdia", b"minf", b"stbl", b"stco"], trak)
        chunk_offsets = list(struct.unpack_from(f">{struct.unpack_from('>I', stco, 12)[0]}I", stco, 16))
    except AssertionError:
        co64 = _box(blob, [b"mdia", b"minf", b"stbl", b"co64"], trak)
        chunk_offsets = list(struct.unpack_from(f">{struct.unpack_from('>I', co64, 12)[0]}Q", co64, 16))

    counts: list[int] = []
    for position, (first, per_chunk, _) in enumerate(entries):
        last = entries[position + 1][0] - 1 if position + 1 < len(entries) else len(chunk_offsets)
        counts += [per_chunk] * (last - first + 1)
    assert len(counts) == len(chunk_offsets)

    out = bytearray()
    cursor = 0
    for chunk_index, chunk_offset in enumerate(chunk_offsets):
        position = chunk_offset
        for _ in range(counts[chunk_index]):
            size = sizes[cursor]
            out += blob[position : position + size]
            position += size
            cursor += 1
    assert cursor == len(sizes)
    return bytes(out)


def stream(*, seed: int, total: int) -> bytes:
    """与 ``fmp4.build`` 生成的样本数据完全一致的期望值。"""
    return bytes([(i * 7 + 3 + seed) % 251 for i in range(total)])


# --------------------------------------------------------------------------- #
# 解析源分片
# --------------------------------------------------------------------------- #


def test_parse_reads_the_source_structure(tmp_path: Path) -> None:
    fragment = fmp4.build(sizes=[100, 50, 70], durations=[512, 512, 512], ctos=[0, 100, 0])
    path = tmp_path / "v.m4s"
    fragment.write(path)

    source = mp4.parse(path)
    try:
        assert source.timescale == 16000
        assert source.handler == b"vide"
        assert source.codec_brand == b"avc1"
        assert len(source.samples) == 3
        assert [(s.size, s.duration, s.cto) for s in source.samples] == [
            (100, 512, 0),
            (50, 512, 100),
            (70, 512, 0),
        ]
        assert [s.dts for s in source.samples] == [0, 512, 1024]
        # 分片里第一个样本带 first_sample_flags，其余是 sample_is_non_sync
        assert [s.sync for s in source.samples] == [True, False, False]
        assert source.media_duration == 1536
    finally:
        source.close()


def test_parse_walks_every_fragment(tmp_path: Path) -> None:
    """一个分片文件里通常有几百个 ``moof``；漏掉后面的会静默丢样本。"""
    fragment = fmp4.build(sizes=[10] * 7, durations=[100] * 7, per_fragment=2)
    path = tmp_path / "v.m4s"
    fragment.write(path)

    source = mp4.parse(path)
    try:
        assert len(source.samples) == 7
        assert [s.dts for s in source.samples] == [0, 100, 200, 300, 400, 500, 600]
        # 每个 moof 的首样本都是同步点
        assert [s.sync for s in source.samples] == [True, False, True, False, True, False, True]
    finally:
        source.close()


def test_parse_reports_a_clear_error_for_garbage(tmp_path: Path) -> None:
    path = tmp_path / "junk.m4s"
    path.write_bytes(b"this is not an mp4 at all, not even close")
    with pytest.raises(BiliError) as excinfo:
        mp4.parse(path)
    assert "fMP4" in excinfo.value.message or "样本" in excinfo.value.message


def test_parse_reports_a_clear_error_for_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BiliError):
        mp4.parse(tmp_path / "nope.m4s")


def test_parse_reports_a_clear_error_for_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.m4s"
    path.write_bytes(b"")
    with pytest.raises(BiliError):
        mp4.parse(path)


# --------------------------------------------------------------------------- #
# 第 1 条规则：交织顺序按编辑列表校正后的 DTS
# --------------------------------------------------------------------------- #


def test_interleave_follows_the_edit_list_and_puts_video_first(tmp_path: Path) -> None:
    """视频轨带 ``media_time=1600``（0.1 s）的偏移，排序必须减去它。

    用原始 dts 排序会让音频在**开头**就插进来，产物能播但包序与 ffmpeg 完全不同。
    这也是 B站 音画对齐的依据，不是可以"简化"掉的细节。
    """
    video_path, audio_path = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(
        handler=b"vide", codec=b"avc1", timescale=16000,
        sizes=[10] * 4, durations=[1600] * 4, media_time=1600,
    ).write(video_path)
    fmp4.build(
        handler=b"soun", codec=b"mp4a", timescale=16000,
        sizes=[10] * 4, durations=[1600] * 4, media_time=0,
    ).write(audio_path)

    sources = [mp4.parse(video_path), mp4.parse(audio_path)]
    try:
        order = [track for track, _ in mp4.interleave(sources)]
    finally:
        for source in sources:
            source.close()

    # 显示 dts：视频 −1600,0,1600,3200　音频 0,1600,3200,4800；同时刻视频优先
    assert order == [0, 0, 1, 0, 1, 0, 1, 1]


def test_interleave_never_compares_rounded_float_times(tmp_path: Path) -> None:
    """16000 与 44100 互质，两条时间轴只有在 7056000 的整数刻度上才可比。

    各自除一遍再比浮点是不行的：0.1 s 这种"看起来相等"的键会因舍入误差排出不同
    顺序。统一乘到公共刻度上做整数比较，结果就只取决于真实时间。
    """
    video_path, audio_path = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(timescale=16000, sizes=[10] * 3, durations=[1600] * 3).write(video_path)
    fmp4.build(
        handler=b"soun", codec=b"mp4a", timescale=44100, sizes=[10] * 3, durations=[4410] * 3
    ).write(audio_path)

    sources = [mp4.parse(video_path), mp4.parse(audio_path)]
    try:
        assert sources[0].timescale == 16000 and sources[1].timescale == 44100
        order = [track for track, _ in mp4.interleave(sources)]
    finally:
        for source in sources:
            source.close()
    # 1600/16000 与 4410/44100 都是 0.1 s，三条样本的显示时刻完全重合；
    # 时刻相同时恒为视频先出，所以是严格交替。
    assert order == [0, 1, 0, 1, 0, 1]


# --------------------------------------------------------------------------- #
# 第 2~4 条规则：产物结构
# --------------------------------------------------------------------------- #


def test_merge_writes_ftyp_then_moov_then_mdat(tmp_path: Path) -> None:
    """渐进式布局：``moov`` 必须在 ``mdat`` 前面，否则没法边下边播。"""
    video = tmp_path / "v.m4s"
    fmp4.build(sizes=[100, 50], durations=[512, 512]).write(video)
    dest = mp4.merge(video=video, audio=None, dest=tmp_path / "out.mp4")

    blob = dest.read_bytes()
    kinds = [kind for _, _, kind in _iter(blob, 0, len(blob))]
    assert kinds == [b"ftyp", b"moov", b"mdat"]
    assert b"moof" not in blob
    assert not (tmp_path / "out.mp4.part").exists()


def test_merge_copies_sample_data_byte_for_byte(tmp_path: Path) -> None:
    video, audio = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(sizes=[100, 50, 70], durations=[512] * 3, seed=0).write(video)
    fmp4.build(
        handler=b"soun", codec=b"mp4a", timescale=44100, sizes=[30, 40], durations=[1024] * 2, seed=99
    ).write(audio)

    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")

    assert track_sample_bytes(dest, 0) == stream(seed=0, total=220)
    assert track_sample_bytes(dest, 1) == stream(seed=99, total=70)
    # mdat 正文恰好是两者的总长，没有多余填充
    start, end = _payload(dest.read_bytes(), [b"mdat"])
    assert end - start == 290


def test_merge_renumbers_the_audio_track(tmp_path: Path) -> None:
    """两条源轨的 ``track_id`` 都是 1；不重编号就会有两条轨抢同一个编号。"""
    video, audio = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(sizes=[10, 10], durations=[512] * 2).write(video)
    fmp4.build(handler=b"soun", codec=b"mp4a", timescale=44100, sizes=[10], durations=[1024]).write(audio)

    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    blob = dest.read_bytes()

    ids = [struct.unpack_from(">I", _box(blob, [b"tkhd"], trak), 20)[0] for trak in _tracks(blob)]
    assert ids == [1, 2]
    # next_track_id 要跟着走，否则新加的轨会撞号
    mvhd = _box(blob, [b"moov", b"mvhd"])
    assert struct.unpack_from(">I", mvhd, len(mvhd) - 4)[0] == 3
    # moov 里不该再有 mvex/trex——它们是分片容器的标记
    assert b"mvex" not in blob and b"trex" not in blob


def test_merge_sets_real_durations(tmp_path: Path) -> None:
    """分片源里 ``mvhd``/``tkhd``/``elst`` 的时长都是 0，必须填成真值。

    ``elst`` 尤其关键：留 0 会让部分播放器把这条轨当成空的。
    """
    video, audio = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(timescale=16000, sizes=[10] * 4, durations=[16000] * 4, media_time=1600).write(video)
    fmp4.build(
        handler=b"soun", codec=b"mp4a", timescale=44100, sizes=[10] * 4, durations=[22050] * 4
    ).write(audio)

    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    blob = dest.read_bytes()

    mvhd = _box(blob, [b"moov", b"mvhd"])
    assert struct.unpack_from(">II", mvhd, 20) == (1000, 4000)  # 最长的一条轨

    tracks = _tracks(blob)
    assert len(tracks) == 2
    for trak, (media_duration, movie_duration) in zip(tracks, [(64000, 4000), (88200, 2000)], strict=True):
        mdhd = _box(blob, [b"mdia", b"mdhd"], trak)
        assert struct.unpack_from(">I", mdhd, 24)[0] == media_duration
        elst = _box(blob, [b"edts", b"elst"], trak)
        assert struct.unpack_from(">I", elst, 16)[0] == movie_duration
        assert struct.unpack_from(">i", elst, 20)[0] in (1600, 0)  # media_time 原样保留

    info = mp4.probe(dest)
    assert info is not None
    assert info.duration == pytest.approx(4.0)
    assert info.streams[0].width == 640 and info.streams[0].height == 360


def test_merge_rebuilds_ftyp_for_the_video_codec(tmp_path: Path) -> None:
    """两条流的 ``ftyp`` 未必相同——AV1 分片多带一个 ``av01`` 品牌。

    照抄任何一条都会丢信息，所以按编解码器重新合成；``dash`` 这种分片专用品牌
    不该出现在成品里（ffmpeg 也会丢掉它）。
    """
    video, audio = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(codec=b"av01", brands=(b"iso5", b"iso6", b"av01", b"dash", b"mp41")).write(video)
    fmp4.build(
        handler=b"soun", codec=b"mp4a", timescale=44100, brands=(b"iso5", b"iso6", b"mp41", b"dash")
    ).write(audio)

    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    ftyp = _box(dest.read_bytes(), [b"ftyp"])
    assert b"av01" in ftyp
    assert b"dash" not in ftyp


def test_merge_writes_audio_only(tmp_path: Path) -> None:
    audio = tmp_path / "a.m4s"
    fmp4.build(handler=b"soun", codec=b"mp4a", timescale=44100, sizes=[10, 20], durations=[1024] * 2).write(audio)

    dest = mp4.merge(video=None, audio=audio, dest=tmp_path / "out.m4a")
    info = mp4.probe(dest)
    assert info is not None
    assert info.kinds == {"audio"}
    assert track_sample_bytes(dest, 0) == stream(seed=0, total=30)


def test_merge_refuses_an_empty_request(tmp_path: Path) -> None:
    with pytest.raises(BiliError):
        mp4.merge(video=None, audio=None, dest=tmp_path / "out.mp4")


def test_merge_leaves_nothing_behind_when_a_source_is_bad(tmp_path: Path) -> None:
    """失败必须不留半个产物——调用方看到产物存在就会去删分片。"""
    good, bad = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(sizes=[10]).write(good)
    bad.write_bytes(b"garbage" * 100)
    dest = tmp_path / "out.mp4"

    with pytest.raises(BiliError):
        mp4.merge(video=good, audio=bad, dest=dest)

    assert not dest.exists()
    assert not (tmp_path / "out.mp4.part").exists()


# --------------------------------------------------------------------------- #
# 样本表本身
# --------------------------------------------------------------------------- #


def test_stss_is_omitted_when_every_sample_is_a_sync_point(tmp_path: Path) -> None:
    """``stss`` 缺席的含义就是"每帧都能直接解码"，写一张全量表反而是错的信息。"""
    video = tmp_path / "v.m4s"
    fmp4.build(sizes=[10, 20, 30], durations=[512] * 3, per_fragment=1).write(video)
    dest = mp4.merge(video=video, audio=None, dest=tmp_path / "out.mp4")
    stbl = _box(dest.read_bytes(), [b"moov", b"trak", b"mdia", b"minf", b"stbl"])
    assert b"stss" not in stbl


def test_stss_lists_the_non_leading_samples(tmp_path: Path) -> None:
    video = tmp_path / "v.m4s"
    fmp4.build(sizes=[10, 20, 30, 40], durations=[512] * 4, per_fragment=2).write(video)
    dest = mp4.merge(video=video, audio=None, dest=tmp_path / "out.mp4")
    trak = _tracks(dest.read_bytes())[0]
    stss = _box(dest.read_bytes(), [b"mdia", b"minf", b"stbl", b"stss"], trak)
    count = struct.unpack_from(">I", stss, 12)[0]
    assert list(struct.unpack_from(f">{count}I", stss, 16)) == [1, 3]


def _sample(cto: int, duration: int = 512) -> mp4.Sample:
    return mp4.Sample(offset=0, size=10, duration=duration, cto=cto, dts=0, sync=True)


def test_ctts_grows_a_version_field_when_offsets_go_negative() -> None:
    """composition offset 在 version 0 上是无符号的，装不下负值。

    这条只能直接测样本表构造函数：真实 B站 分片的 ``trun`` 是 version 0，本身也
    表达不了负偏移，构造不出这样的输入。
    """
    negative = mp4.build_ctts([_sample(0), _sample(-100), _sample(200)])
    assert negative is not None
    assert negative[8] == 1  # version 1 = 有符号
    # ctts 是 (count, value) 游程的数组，值在 20 + 8i 处
    assert struct.unpack_from(">I", negative, 12)[0] == 3
    assert [struct.unpack_from(">i", negative, 20 + 8 * i)[0] for i in range(3)] == [0, -100, 200]

    positive = mp4.build_ctts([_sample(0), _sample(100)])
    assert positive is not None and positive[8] == 0


def test_ctts_is_omitted_when_every_offset_is_zero() -> None:
    """全零时这个盒子不提供任何信息，省掉能少几百字节。"""
    assert mp4.build_ctts([_sample(0), _sample(0)]) is None


def test_stts_compresses_repeated_durations_into_runs() -> None:
    """逐样本一条记录会让 17 分钟的视频多出几 MB 的 ``stts``。"""
    stts = mp4.build_stts([_sample(0), _sample(0), _sample(0, 999), _sample(0, 999)])
    assert struct.unpack_from(">I", stts, 12)[0] == 2  # run 数
    assert struct.unpack_from(">II", stts, 16) == (2, 512)
    assert struct.unpack_from(">II", stts, 24) == (2, 999)


def test_one_chunk_per_run_of_samples_from_the_same_track(tmp_path: Path) -> None:
    """逐样本一块也能播，但 ``stco`` 会白占每个样本 4 字节。

    块数还必须能在算偏移之前定下来——它决定 ``moov`` 长度，进而决定所有偏移。
    """
    video, audio = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(timescale=16000, sizes=[10] * 4, durations=[1600] * 4).write(video)
    fmp4.build(handler=b"soun", codec=b"mp4a", timescale=16000, sizes=[10] * 4, durations=[1600] * 4).write(audio)

    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    blob = dest.read_bytes()
    # 8 个样本交替排列 → 8 个块；若合成一块就没法按解码顺序落盘
    for trak in _tracks(blob):
        stco = _box(blob, [b"mdia", b"minf", b"stbl", b"stco"], trak)
        assert struct.unpack_from(">I", stco, 12)[0] == 4


# --------------------------------------------------------------------------- #
# probe（取代原来的 ffprobe 调用）
# --------------------------------------------------------------------------- #


def test_probe_returns_none_instead_of_raising(tmp_path: Path) -> None:
    """读不出来 ≠ 文件坏。调用方据此放弃校验，而不是否定一个已合并好的产物。"""
    assert mp4.probe(tmp_path / "missing.mp4") is None
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00" * 64)
    assert mp4.probe(junk) is None
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert mp4.probe(empty) is None


def test_probe_reads_codec_and_dimensions(tmp_path: Path) -> None:
    video, audio = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(codec=b"hvc1", width=1080, height=1440, sizes=[10], durations=[16000]).write(video)
    fmp4.build(
        handler=b"soun", codec=b"mp4a", timescale=44100, width=0, height=0, sizes=[10], durations=[1024]
    ).write(audio)

    info = mp4.probe(mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4"))
    assert info is not None
    kinds = {stream.kind: stream for stream in info.streams}
    assert set(kinds) == {"video", "audio"}
    assert kinds["video"].codec == "hvc1"
    assert (kinds["video"].width, kinds["video"].height) == (1080, 1440)
    assert kinds["audio"].codec == "mp4a"


def test_hev1_is_renamed_to_hvc1(tmp_path: Path) -> None:
    """参数集已经在 ``hvcC`` 里，改名不重新编码，却能救回 QuickTime。"""
    video = tmp_path / "v.m4s"
    fmp4.build(codec=b"hev1", sizes=[10], durations=[512]).write(video)
    dest = mp4.merge(video=video, audio=None, dest=tmp_path / "out.mp4")
    info = mp4.probe(dest)
    assert info is not None and info.streams[0].codec == "hvc1"


# --------------------------------------------------------------------------- #
# 真机集成：合成的分片结构对，真实的分片才说明能播
# --------------------------------------------------------------------------- #

#: 跑过 ``--no-merge --keep-parts`` 之后留下的真实分片目录，按需找。
_REAL_SAMPLES = (
    Path("/tmp/muxprobe/吉罗很快乐"),
    Path("/tmp/muxprobe/vert/邪修美食"),
)


def _real_pair(directory: Path) -> tuple[Path, Path] | None:
    if not directory.is_dir():
        return None
    videos = sorted(directory.glob("*.video.m4s"))
    audios = sorted(directory.glob("*.audio.m4s"))
    if not videos or not audios:
        return None
    return videos[0], audios[0]


def _ffprobe() -> str | None:
    candidates = (shutil.which("ffprobe"), "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


@pytest.mark.parametrize("directory", _REAL_SAMPLES)
def test_merges_a_real_bilibili_fragment(directory: Path, tmp_path: Path) -> None:
    """合成分片证明结构对，真实分片才能证明我们读得懂 B站 的排布。

    ffprobe 在这里是**独立鉴定人**：产物结构若有错，它根本读不出来。它不参与合并，
    所以它的结论不依赖本模块的任何代码。
    """
    pair = _real_pair(directory)
    if pair is None:
        pytest.skip(f"没有真实分片：{directory}（先跑 bclaw <BV> --no-merge --keep-parts）")
    video, audio = pair

    dest = mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4")
    info = mp4.probe(dest)
    assert info is not None
    assert info.kinds == {"video", "audio"}
    assert info.duration > 1.0

    # 分片里的 moof 全部被丢掉、换成一个 moov，所以体积应当是
    # 「样本数据总量 + 一个 moov」，比两个分片之和略小。
    sources = [mp4.parse(video), mp4.parse(audio)]
    try:
        payload = sum(sample.size for source in sources for sample in source.samples)
        counts = {
            "video" if source.handler == b"vide" else "audio": len(source.samples)
            for source in sources
        }
    finally:
        for source in sources:
            source.close()
    assert payload <= dest.stat().st_size <= payload + 2 * 1024 * 1024

    judge = _ffprobe()
    if judge is None:
        return
    result = subprocess.run(
        [
            judge, "-v", "error", "-count_packets",
            "-show_entries", "stream=codec_type,nb_read_packets",
            "-of", "json", str(dest),
        ],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(result.stdout)["streams"]
    assert {stream["codec_type"] for stream in streams} == {"video", "audio"}
    assert {stream["codec_type"]: int(stream["nb_read_packets"]) for stream in streams} == counts


def test_write_runs_coalesces_only_truly_adjacent_samples() -> None:
    """相邻同轨样本合成一段写；一旦源偏移断开就必须另起一段，否则会串字节。"""
    runs = list(
        mp4._write_runs(
            [
                (0, mp4.Sample(offset=100, size=10, duration=1, cto=0, dts=0, sync=True)),
                (0, mp4.Sample(offset=110, size=20, duration=1, cto=0, dts=1, sync=True)),
                # 同轨但不连续：中间隔着别的数据，不能并进上一段
                (0, mp4.Sample(offset=500, size=5, duration=1, cto=0, dts=2, sync=True)),
                (1, mp4.Sample(offset=130, size=7, duration=1, cto=0, dts=3, sync=True)),
            ]
        )
    )
    assert runs == [(0, 100, 30), (0, 500, 5), (1, 130, 7)]


def test_merge_reports_cumulative_progress_ending_at_the_payload_size(tmp_path: Path) -> None:
    """合并回调报的是**累计**字节数，且最后一次必须正好等于样本总字节。

    调用方（CLI 的进度条）直接拿它当 ``completed`` 用，所以既不能是增量，也不能
    在结尾差一截——那会让进度条停在 99% 不动。
    """
    video, audio = tmp_path / "v.m4s", tmp_path / "a.m4s"
    fmp4.build(sizes=[100, 50, 70], durations=[512] * 3, seed=1).write(video)
    fmp4.build(handler=b"soun", codec=b"mp4a", timescale=44100, sizes=[40, 30], durations=[1024] * 2, seed=2).write(
        audio
    )

    seen: list[tuple[int, int]] = []

    def record(done: int, total: int) -> None:
        seen.append((done, total))

    mp4.merge(video=video, audio=audio, dest=tmp_path / "out.mp4", progress=record)

    assert seen, "回调一次都没被调用"
    assert all(done <= total for done, total in seen)
    assert seen[-1][0] == seen[-1][1] == 100 + 50 + 70 + 40 + 30
    assert [done for done, _ in seen] == sorted(done for done, _ in seen), "累计值必须单调不减"


def test_merge_without_a_callback_still_works(tmp_path: Path) -> None:
    """回调是可选的：CLI 在短片（< 512 MB）上根本不传它。"""
    video = tmp_path / "v.m4s"
    fmp4.build().write(video)
    dest = mp4.merge(video=video, audio=None, dest=tmp_path / "out.mp4")
    assert dest.stat().st_size > 0


def test_stco_switches_to_64_bit_offsets_when_needed() -> None:
    """超过 4 GB 时 32 位装不下，必须换成 ``co64``。

    真造一个 4 GB 样片不现实，所以直接测表构造函数：这里真正的风险不是"算错偏移"，
    而是**选错盒子**——写成 ``stco`` 会把偏移截成低 32 位，产物从此打不开，而合并
    阶段一声不吭。
    """
    small = mp4.build_stco([100, 200], wide=False)
    assert small[4:8] == b"stco"
    assert list(struct.unpack_from(">2I", small, 16)) == [100, 200]

    big = 0x1_0000_0000 + 7
    large = mp4.build_stco([big], wide=True)
    assert large[4:8] == b"co64"
    assert list(struct.unpack_from(">1Q", large, 16)) == [big]


@pytest.mark.parametrize(
    ("start", "body_size", "expected"),
    [
        (100, 100, False),
        (0, mp4._OFFSET_LIMIT - 1, False),  # 最后一个块的起点仍在界内
        (0, mp4._OFFSET_LIMIT, True),  # 正好压线：偏移等于上限，32 位存不下
        (0, mp4._OFFSET_LIMIT + 1, True),
    ],
)
def test_needs_wide_offsets_boundary(start: int, body_size: int, expected: bool) -> None:
    """这条界限只能靠算术验证，所以每个边界值都写死在这里。"""
    assert mp4.needs_wide_offsets(start, body_size) is expected
