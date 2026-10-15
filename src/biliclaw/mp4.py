"""纯 Python 的 MP4 读写内核（FR-05）。

**为什么自己写**：合并本来只需要把两条流原样搬进一个容器，却让整个工具背上
「必须先装 ffmpeg」的前提。B站的 DASH 分片本身就是自包含的 fMP4，样本数据
一个字节都不用改，重排容器结构就够了——这件事用不着一个 52 MB 的外部程序。

**产物形态：渐进式 MP4**（单个 ``moov`` + 完整样本表），不是分片式。这与
``ffmpeg -c copy -movflags +faststart`` 的形态一致，兼容性最好；额外的好处是
``mvex`` / ``trex`` / ``tfhd`` / ``moof`` 全都不会出现在产物里，于是「音频轨
重编号」这整类问题自动消失，只剩 ``tkhd.track_id`` 一处要写。

**正确性依据**：本模块的产物已与 ffmpeg 产物做过逐包对拍（6 组样片：AVC /
HEVC / AV1 × 横竖屏 × 单双轨），``ffprobe -show_packets`` 的
``(stream_index, pts, dts, size, flags)`` 序列**完全相同**，AVFoundation 全片
解码的帧数与字节数也完全相同。细节见 ``docs/落地计划.md`` §5 P0。

**四条容易踩空的规则**（都是实测踩出来的，改动时请勿"简化"）：

1. 交织顺序按 ``dts - elst.media_time`` 全局归并，同一时刻**视频优先**。
   用原始 ``dts`` 排序是错的——视频轨带 ``media_time=1600`` 的偏移。
2. ``ftyp`` 必须按编解码器**重新合成**。B站 AV1 分片比 AVC/HEVC 多带一个
   ``av01`` 品牌，所以"两条流的 ftyp 相同"这个前提根本不成立。
3. 样本要按**文件里连续同轨**合块。逐样本一块也能播，但 ``stco`` 白占每个
   样本 4 字节（实测单轨文件会因此大 120~180 KB）。
4. 块的**个数**决定 ``moov`` 长度，进而决定所有偏移。所以必须先分块，再按
   占位偏移构造一次 ``moov`` 求长度，最后回填真实偏移。
"""

from __future__ import annotations

import math
import mmap
import os
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BiliError

__all__ = ["Info", "Stream", "merge", "probe"]

#: 电影头（``mvhd``/``tkhd``/``elst``）的时间刻度。沿用源分片的值，不学 ffmpeg 升到
#: 7056000——那需要把三个盒子都升到 version 1（32 位存不下），代价远大于收益：现在
#: 的差异只是容器时长被量化到 1 ms，而样本表的 ``stts`` 是精确的。
MOVIE_TIMESCALE = 1000

#: ``moov`` 里这些盒子是**原样搬运**的，不做任何解析。
_COPIED_BOXES = ("ftyp", "mvhd", "tkhd", "edts", "mdhd", "hdlr", "dinf", "stsd", "udta")


# --------------------------------------------------------------------------- #
# 盒子原语
# --------------------------------------------------------------------------- #


def iter_boxes(data, start: int, end: int) -> Iterator[tuple[int, int, bytes]]:
    """遍历 ``[start, end)`` 内的**同级**盒子，产出 ``(偏移, 长度, 类型)``。

    长度字段的三种写法都要认：32 位、``size==1`` 时的 64 位扩展、以及
    ``size==0`` 表示"一直到父盒子末尾"。
    """
    offset = start
    while offset + 8 <= end:
        size = struct.unpack_from(">I", data, offset)[0]
        kind = bytes(data[offset + 4 : offset + 8])
        header = 8
        if size == 1:
            if offset + 16 > end:
                raise ValueError(f"盒子 {kind!r} 的 64 位长度字段越界")
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            raise ValueError(f"盒子 {kind!r} 声明长度 {size} 越界（@{offset}，上限 {end}）")
        yield offset, size, kind
        offset += size


def find_box(data, path: list[bytes], start: int = 0, end: int | None = None) -> tuple[int, int] | None:
    """按路径（如 ``[b"moov", b"trak", b"mdhd"]``）定位盒子，返回 ``(偏移, 长度)``。

    ``start`` / ``end`` 是**要搜索的同级区域**。注意它不是某个盒子的 ``(偏移, 长度)``：
    要搜一个盒子的子盒，得传 ``(offset + 8, offset + length)``。
    """
    end = len(data) if end is None else end
    for depth, name in enumerate(path):
        for offset, size, kind in iter_boxes(data, start, end):
            if kind == name:
                if depth == len(path) - 1:
                    return offset, size
                start, end = offset + 8, offset + size
                break
        else:
            return None
    return None


def _slice(data, found: tuple[int, int] | None) -> bytes:
    if not found:
        return b""
    offset, size = found
    return bytes(data[offset : offset + size])


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def full_box(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def full_box_header(data, offset: int) -> tuple[int, int, int]:
    """读出 FullBox 的 ``(version, flags, 正文起点)``。"""
    return data[offset + 8], int.from_bytes(data[offset + 9 : offset + 12], "big"), offset + 12


# --------------------------------------------------------------------------- #
# 样本与源流
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Sample:
    """一个样本（一帧视频 / 一个 AAC 包）。"""

    offset: int  #: 在**源文件**里的字节偏移
    size: int
    duration: int  #: 解码时长，单位是所在轨的 media timescale
    cto: int  #: composition time offset（pts − dts）
    dts: int  #: 解码时间，已含 ``tfdt`` 基准，但**未**做编辑列表校正
    sync: bool  #: 是否关键帧


@dataclass(slots=True)
class Source:
    """一条流的分片文件，已解析出完整样本表。"""

    path: Path
    data: mmap.mmap
    handle: object
    timescale: int
    handler: bytes
    codec_brand: bytes
    samples: list[Sample] = field(default_factory=list)
    #: 原样搬运的盒子
    ftyp: bytes = b""
    mvhd: bytes = b""
    tkhd: bytes = b""
    edts: bytes = b""
    mdhd: bytes = b""
    hdlr: bytes = b""
    media_header: bytes = b""
    dinf: bytes = b""
    stsd: bytes = b""
    udta: bytes = b""

    @property
    def media_duration(self) -> int:
        return sum(sample.duration for sample in self.samples)

    def movie_duration(self) -> int:
        """媒体时长换算到电影时间刻度，向上取整——宁可多一帧也不要截掉尾巴。"""
        if not self.timescale:
            return 0
        return -(-self.media_duration * MOVIE_TIMESCALE // self.timescale)

    def elst_media_time(self) -> int:
        """编辑列表的 ``media_time``，即显示时间轴相对解码时间轴的偏移。

        B站视频轨的 1600 就是它（0.1 s，用来对齐音画）。第 1 条规则要用这个值。
        """
        if not self.edts:
            return 0
        found = find_box(self.edts, [b"edts", b"elst"])
        if not found:
            return 0
        offset = found[0]
        version = self.edts[offset + 8]
        if struct.unpack_from(">I", self.edts, offset + 12)[0] == 0:
            return 0  # entry_count == 0
        cursor = offset + 20 if version == 0 else offset + 24
        return struct.unpack_from(">i" if version == 0 else ">q", self.edts, cursor)[0]

    def close(self) -> None:
        try:
            self.data.close()
        finally:
            self.handle.close()


def _parse_trex(data) -> tuple[int, int, int]:
    """取 ``mvex/trex`` 的缺省值——片段里的 ``trun`` 常常整段省略它们。"""
    found = find_box(data, [b"moov", b"mvex", b"trex"])
    if not found:
        return 0, 0, 0
    _, _, duration, size, flags = struct.unpack_from(">IIIII", data, found[0] + 12)
    return duration, size, flags


def _parse_mdhd_timescale(mdhd: bytes) -> int:
    if len(mdhd) < 32:
        return 0
    return struct.unpack_from(">I", mdhd, 20 if mdhd[8] == 0 else 28)[0]


def _parse_fragment(data, moof_offset: int, moof_size: int, defaults: tuple[int, int, int]) -> list[Sample]:
    """把一个 ``moof`` 里的所有样本还原出来。"""
    found: list[Sample] = []

    for traf_offset, traf_size, traf_kind in iter_boxes(data, moof_offset + 8, moof_offset + moof_size):
        if traf_kind != b"traf":
            continue

        values = {"duration": defaults[0], "size": defaults[1], "flags": defaults[2]}
        base_offset = moof_offset  # default-base-is-moof 时基准就是 moof 起点
        decode_time = 0
        run_at: int | None = None

        for child_offset, _, child_kind in iter_boxes(data, traf_offset + 8, traf_offset + traf_size):
            if child_kind == b"tfhd":
                _, flags, cursor = full_box_header(data, child_offset)
                cursor += 4  # track_ID，单轨源里恒为 1，不需要
                if flags & 0x000001:  # base-data-offset-present
                    base_offset = struct.unpack_from(">Q", data, cursor)[0]
                    cursor += 8
                if flags & 0x000002:  # sample-description-index-present
                    cursor += 4
                for bit, name in ((0x000008, "duration"), (0x000010, "size"), (0x000020, "flags")):
                    if flags & bit:
                        values[name] = struct.unpack_from(">I", data, cursor)[0]
                        cursor += 4
            elif child_kind == b"tfdt":
                version, _, cursor = full_box_header(data, child_offset)
                decode_time = struct.unpack_from(">I" if version == 0 else ">Q", data, cursor)[0]
            elif child_kind == b"trun":
                run_at = child_offset

        if run_at is not None:
            found += _parse_trun(data, run_at, base_offset, decode_time, values)

    return found


def _parse_trun(data, offset: int, base_offset: int, decode_time: int, values: dict) -> list[Sample]:
    version, flags, cursor = full_box_header(data, offset)
    count = struct.unpack_from(">I", data, cursor)[0]
    cursor += 4

    data_offset = 0
    if flags & 0x000001:
        data_offset = struct.unpack_from(">i", data, cursor)[0]
        cursor += 4
    first_sample_flags: int | None = None
    if flags & 0x000004:
        first_sample_flags = struct.unpack_from(">I", data, cursor)[0]
        cursor += 4

    payload = base_offset + data_offset
    samples: list[Sample] = []
    for index in range(count):
        duration, sample_size, sample_flags, cto = (
            values["duration"],
            values["size"],
            values["flags"],
            0,
        )
        if flags & 0x000100:
            duration = struct.unpack_from(">I", data, cursor)[0]
            cursor += 4
        if flags & 0x000200:
            sample_size = struct.unpack_from(">I", data, cursor)[0]
            cursor += 4
        if flags & 0x000400:
            sample_flags = struct.unpack_from(">I", data, cursor)[0]
            cursor += 4
        if flags & 0x000800:
            cto = struct.unpack_from(">i" if version == 1 else ">I", data, cursor)[0]
            cursor += 4

        if index == 0 and first_sample_flags is not None:
            sample_flags = first_sample_flags

        samples.append(
            Sample(
                offset=payload,
                size=sample_size,
                duration=duration,
                cto=cto,
                dts=decode_time,
                # sample_flags 的 bit16 是 sample_is_non_sync_sample
                sync=not (sample_flags & 0x00010000),
            )
        )
        payload += sample_size
        decode_time += duration
    return samples


def parse(path: Path) -> Source:
    """解析一个 DASH 分片文件，还原出完整样本表。"""
    if path is None:
        raise BiliError("没有可解析的流")
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise BiliError(f"无法读取分片文件：{path}", hint=str(exc)) from exc

    try:
        if path.stat().st_size == 0:
            raise BiliError(f"分片文件是空的：{path}")
        data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError) as exc:
        handle.close()
        raise BiliError(f"无法映射分片文件：{path}", hint=str(exc)) from exc

    try:
        source = Source(
            path=path, data=data, handle=handle, timescale=0, handler=b"", codec_brand=b""
        )
        source.ftyp = _slice(data, find_box(data, [b"ftyp"]))
        source.mvhd = _slice(data, find_box(data, [b"moov", b"mvhd"]))
        source.tkhd = _slice(data, find_box(data, [b"moov", b"trak", b"tkhd"]))
        source.edts = _slice(data, find_box(data, [b"moov", b"trak", b"edts"]))
        source.mdhd = _slice(data, find_box(data, [b"moov", b"trak", b"mdia", b"mdhd"]))
        source.hdlr = _slice(data, find_box(data, [b"moov", b"trak", b"mdia", b"hdlr"]))
        source.dinf = _slice(data, find_box(data, [b"moov", b"trak", b"mdia", b"minf", b"dinf"]))
        source.stsd = _slice(data, find_box(data, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"]))
        source.udta = _slice(data, find_box(data, [b"moov", b"udta"]))
        for kind in (b"vmhd", b"smhd"):
            found = find_box(data, [b"moov", b"trak", b"mdia", b"minf", kind])
            if found:
                source.media_header = _slice(data, found)
                break

        source.handler = source.hdlr[16:20] if len(source.hdlr) >= 20 else b""
        source.codec_brand = source.stsd[20:24] if len(source.stsd) >= 24 else b""
        source.timescale = _parse_mdhd_timescale(source.mdhd)
        # 参数集已经放在 hvcC 里，所以 hev1 → hvc1 只是改个名字，却能救回 QuickTime
        source.stsd = patch_hev1_to_hvc1(source.stsd)

        defaults = _parse_trex(data)
        for offset, size, kind in iter_boxes(data, 0, len(data)):
            if kind == b"moof":
                source.samples += _parse_fragment(data, offset, size, defaults)
    except (ValueError, struct.error, IndexError) as exc:
        source.close()
        raise BiliError(f"分片文件不是可识别的 fMP4：{path}", hint=str(exc)) from exc

    if not source.samples:
        source.close()
        raise BiliError(f"分片文件里没有找到任何样本：{path}")
    if not source.timescale:
        source.close()
        raise BiliError(f"分片文件缺少有效的时间刻度：{path}")
    return source


# --------------------------------------------------------------------------- #
# 样本表构造
# --------------------------------------------------------------------------- #


def _run_length(values, fmt: str) -> bytes:
    """把一串值压成 ``(count, value)`` 游程，这是 ``stts``/``ctts`` 的通用形状。"""
    runs: list[list[int]] = []
    for value in values:
        if runs and runs[-1][1] == value:
            runs[-1][0] += 1
        else:
            runs.append([1, value])
    return struct.pack(">I", len(runs)) + b"".join(
        struct.pack(">I", count) + struct.pack(fmt, value) for count, value in runs
    )


def build_stts(samples: list[Sample]) -> bytes:
    return full_box(b"stts", 0, 0, _run_length((s.duration for s in samples), ">I"))


def build_stss(samples: list[Sample]) -> bytes | None:
    """关键帧索引。**全同步样本时不写这个盒子**——它的缺席含义就是"每帧都是同步点"。"""
    if all(sample.sync for sample in samples):
        return None
    indices = [index + 1 for index, sample in enumerate(samples) if sample.sync]
    payload = struct.pack(">I", len(indices)) + b"".join(struct.pack(">I", i) for i in indices)
    return full_box(b"stss", 0, 0, payload)


def build_ctts(samples: list[Sample]) -> bytes | None:
    """composition offset 表。全零时省略；出现负值必须用 version 1（无符号字段装不下）。"""
    if all(sample.cto == 0 for sample in samples):
        return None
    negative = any(sample.cto < 0 for sample in samples)
    version = 1 if negative else 0
    return full_box(b"ctts", version, 0, _run_length((s.cto for s in samples), ">i" if negative else ">I"))


def build_stsz(samples: list[Sample]) -> bytes:
    sizes = b"".join(struct.pack(">I", sample.size) for sample in samples)
    return full_box(b"stsz", 0, 0, struct.pack(">II", 0, len(samples)) + sizes)


def build_stsc(counts: list[int]) -> bytes:
    """描述"第几个块装几个样本"。``counts`` 是每块的样本数。"""
    if not counts:
        return full_box(b"stsc", 0, 0, struct.pack(">I", 0))
    runs: list[tuple[int, int]] = []
    for index, count in enumerate(counts, start=1):
        if runs and runs[-1][1] == count:
            continue
        runs.append((index, count))
    payload = struct.pack(">I", len(runs)) + b"".join(
        struct.pack(">III", first, count, 1) for first, count in runs
    )
    return full_box(b"stsc", 0, 0, payload)


def build_stco(offsets: list[int], wide: bool) -> bytes:
    """块偏移表。超过 32 位就自动改用 64 位的 ``co64``。"""
    if wide:
        body = b"".join(struct.pack(">Q", value) for value in offsets)
        return full_box(b"co64", 0, 0, struct.pack(">I", len(offsets)) + body)
    body = b"".join(struct.pack(">I", value) for value in offsets)
    return full_box(b"stco", 0, 0, struct.pack(">I", len(offsets)) + body)


# --------------------------------------------------------------------------- #
# 定长字段的原地改写
#
# 一律「复制原字节 → 只覆盖要改的字段」，而不是重新构造盒子。这样源文件里那些
# 我们不关心的字段（语言、矩阵、音量、色域……）都会原样保留，不需要逐个理解。
# --------------------------------------------------------------------------- #


def patch_mdhd(mdhd: bytes, duration: int) -> bytes:
    raw = bytearray(mdhd)
    version = raw[8]
    struct.pack_into(">I" if version == 0 else ">Q", raw, 24 if version == 0 else 32, duration)
    return bytes(raw)


def patch_tkhd(tkhd: bytes, *, track_id: int, duration: int) -> bytes:
    raw = bytearray(tkhd)
    version = raw[8]
    struct.pack_into(">I", raw, 20 if version == 0 else 28, track_id)
    struct.pack_into(">I" if version == 0 else ">Q", raw, 28 if version == 0 else 36, duration)
    return bytes(raw)


def patch_elst(edts: bytes, duration: int) -> bytes:
    """把编辑列表的 ``segment_duration`` 从 0 改成真实时长。

    分片源里这个字段是 0（含义是"一直到轨道结束"），但渐进式 MP4 的播放器要靠它
    决定这条轨播多久；写 0 会让部分播放器当成空轨道。
    """
    if not edts:
        return b""
    found = find_box(edts, [b"edts", b"elst"])
    if not found:
        return edts
    offset = found[0]
    raw = bytearray(edts)
    version = raw[offset + 8]
    if struct.unpack_from(">I", raw, offset + 12)[0]:
        struct.pack_into(">I" if version == 0 else ">Q", raw, offset + 16, duration)
    return bytes(raw)


def patch_mvhd(mvhd: bytes, *, duration: int, next_track_id: int) -> bytes:
    raw = bytearray(mvhd)
    version = raw[8]
    struct.pack_into(">I" if version == 0 else ">Q", raw, 24 if version == 0 else 32, duration)
    struct.pack_into(">I", raw, len(raw) - 4, next_track_id)
    return bytes(raw)


def patch_hev1_to_hvc1(stsd: bytes) -> bytes:
    """``hev1`` → ``hvc1``：参数集在 ``hvcC`` 里时这是纯改名，而 QuickTime 只认后者。

    实测 B站 HEVC 分片本身已经是 ``hvc1``，所以这一步现在是安全网而非必需。
    """
    return stsd.replace(b"hev1", b"hvc1")


# --------------------------------------------------------------------------- #
# moov 组装
# --------------------------------------------------------------------------- #


def build_ftyp(sources: list[Source]) -> bytes:
    """按编解码器**重新合成** ``ftyp``，而不是照抄某一条流的。

    B站的 AV1 分片比 AVC/HEVC 多带一个 ``av01`` 兼容品牌（36 字节 vs 32 字节），
    所以"两条流的 ftyp 逐字节相同"这个前提根本不成立。``ftyp`` 只是一句"本文件
    兼容哪些规范"的声明，按编解码器重新生成才是正确做法——ffmpeg 也是这么做的
    （它统一产出 ``isom`` + 编解码器品牌 + ``mp41``，把 ``dash`` 丢掉）。
    """
    brand = b"mp41"
    for source in sources:
        if source.handler == b"vide" and source.codec_brand:
            brand = source.codec_brand
            break
    ordered: list[bytes] = []
    for candidate in (b"isom", b"iso2", brand, b"mp41"):
        if candidate not in ordered:
            ordered.append(candidate)
    return (
        struct.pack(">I", 16 + 4 * len(ordered))
        + b"ftyp"
        + b"isom"  # 主品牌
        + struct.pack(">I", 0x200)  # 次版本
        + b"".join(ordered)
    )


def build_stbl(source: Source, offsets: list[int], counts: list[int], wide: bool) -> bytes:
    parts = [source.stsd, build_stts(source.samples)]
    for extra in (build_stss(source.samples), build_ctts(source.samples)):
        if extra:
            parts.append(extra)
    parts.append(build_stsc(counts))
    parts.append(build_stsz(source.samples))
    parts.append(build_stco(offsets, wide))
    return box(b"stbl", b"".join(parts))


def build_trak(
    source: Source, offsets: list[int], counts: list[int], wide: bool, *, track_id: int, duration: int
) -> bytes:
    minf = box(b"minf", source.media_header + source.dinf + build_stbl(source, offsets, counts, wide))
    mdia = box(b"mdia", patch_mdhd(source.mdhd, source.media_duration) + source.hdlr + minf)
    edts = patch_elst(source.edts, duration)
    tkhd = patch_tkhd(source.tkhd, track_id=track_id, duration=duration)
    return box(b"trak", tkhd + edts + mdia)


def build_moov(
    sources: list[Source], offsets: list[list[int]], counts: list[list[int]], *, wide: bool
) -> bytes:
    durations = [source.movie_duration() for source in sources]
    traks = [
        build_trak(
            source,
            offsets[index],
            counts[index],
            wide,
            track_id=index + 1,
            duration=durations[index],
        )
        for index, source in enumerate(sources)
    ]
    mvhd = patch_mvhd(sources[0].mvhd, duration=max(durations), next_track_id=len(sources) + 1)
    # ``udta`` 里是 ``©too`` 之类的出处信息，留着比丢掉更友好
    return box(b"moov", mvhd + b"".join(traks) + sources[0].udta)


def interleave(sources: list[Source]) -> list[tuple[int, Sample]]:
    """按**编辑列表校正后的显示 DTS**全局归并，同一时刻视频轨优先（规则 1）。

    比较 ``(dts - elst.media_time) / timescale``，用整数交叉相乘避免浮点误差。
    必须用**显示**时间轴：B站视频轨带 ``media_time=1600``，不减去它的话音频会在
    开头就插进来，产物虽然能播，却与 ffmpeg 的包序完全不同。
    """
    common = math.lcm(*(source.timescale for source in sources))
    scales = [common // source.timescale for source in sources]
    media_times = [source.elst_media_time() for source in sources]

    keyed = [
        ((sample.dts - media_times[track]) * scales[track], track, sample)
        for track, source in enumerate(sources)
        for sample in source.samples
    ]
    keyed.sort(key=lambda item: (item[0], item[1]))

    layout = [(track, sample) for _, track, sample in keyed]
    for track, source in enumerate(sources):
        if [sample for t, sample in layout if t == track] != source.samples:
            raise BiliError(f"轨 {track} 的样本顺序在重排时被打乱了")
    return layout


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #


def _chunk_plan(layout: list[tuple[int, Sample]], track_count: int) -> tuple[list[list[int]], list[list[int]]]:
    """按"文件里连续同轨"分块（规则 3），返回 ``(每块的样本数, 每块的首样本下标)``。

    只取决于交错顺序，与文件偏移无关——所以可以在算偏移之前先定下来，这正是规则 4
    要求的顺序。
    """
    counts: list[list[int]] = [[] for _ in range(track_count)]
    starts: list[list[int]] = [[] for _ in range(track_count)]
    index = 0
    while index < len(layout):
        track = layout[index][0]
        end = index
        while end < len(layout) and layout[end][0] == track:
            end += 1
        counts[track].append(end - index)
        starts[track].append(index)
        index = end
    return counts, starts


#: 32 位块偏移的上限。最后一个块的**起点**越过它就得换 ``co64``。
_OFFSET_LIMIT = 0xFFFFFFFF


def needs_wide_offsets(start: int, body_size: int) -> bool:
    """块偏移会不会越过 32 位，从而必须改用 ``co64``。

    单独拎出来是因为**它无法用真实文件验证**（那要 4 GB 以上的样片），而选错的后果
    又特别难查：``stco`` 会把偏移截断成低 32 位，产物从此打不开，且合并阶段不会报
    任何错。有了这个函数，边界至少可以被单元测试钉住。
    """
    return start + body_size >= _OFFSET_LIMIT


def _write_runs(layout: list[tuple[int, Sample]]) -> Iterator[tuple[int, int, int]]:
    """把交织后的样本表压成「同一条轨、且在源文件里连续」的区段，返回 ``(轨, 源偏移, 长度)``。

    实测（129 MB / 77,534 样本）：压成 63,728 段，中位段长只有一个样本 818 B——**省下的
    系统调用很有限**，因为 B 帧的存在让显示顺序与源文件顺序不再一致，同一分片里的样本
    被重排后大半都不再相邻。所以这里的主要收益是**给进度上报一个天然的粒度**，而不是
    性能；真正的速度来自 ``mmap`` 与切片本身（129 MB 约 0.25 s）。
    """
    index = 0
    while index < len(layout):
        track, first = layout[index]
        start = first.offset
        length = first.size
        index += 1
        while index < len(layout):
            next_track, sample = layout[index]
            if next_track != track or sample.offset != start + length:
                break
            length += sample.size
            index += 1
        yield track, start, length


def merge(
    *,
    video: Path | None,
    audio: Path | None,
    dest: Path,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """把已下载的两条流合并成渐进式 MP4。任一路为空时只封装另一路。

    产物先写到 ``dest.part`` 再改名，因此**失败不会留下半个文件**——这一点很重要：
    调用方看到产物存在就会去删分片。

    视频轨排在第 1 轨、音频轨第 2 轨（``tkhd.track_id`` 与 ``mvhd.next_track_id``
    同步更新），与 ffmpeg 的约定一致。

    ``progress`` 每写完一段连续数据调用一次，参数是 ``(已写字节, 总字节)``。它是可选的：
    129 MB 的合并只要 0.25 s，为它拉一根进度条只会闪烁——**长片才需要**，判据在 CLI 那边。
    """
    paths = [path for path in (video, audio) if path is not None]
    if not paths:
        raise BiliError("没有可合并的音视频流")

    sources: list[Source] = []
    part = dest.with_name(dest.name + ".part")
    try:
        sources = [parse(path) for path in paths]
        ftyp = build_ftyp(sources)
        layout = interleave(sources)
        counts, chunk_starts = _chunk_plan(layout, len(sources))

        def assemble(wide: bool, offsets: list[list[int]]) -> bytes:
            return build_moov(sources, offsets, counts, wide=wide)

        # 先用占位偏移构造一次 moov 求长度。块的**个数**必须是真的，否则长度会变。
        body_size = sum(sample.size for _, sample in layout)
        placeholders = [[0] * len(counts[track]) for track in range(len(sources))]
        blob = assemble(False, placeholders)
        start = len(ftyp) + len(blob) + 8
        wide = needs_wide_offsets(start, body_size)
        if wide:
            blob = assemble(True, placeholders)
            start = len(ftyp) + len(blob) + 8

        positions: list[int] = []
        cursor = start
        for _, sample in layout:
            positions.append(cursor)
            cursor += sample.size
        if cursor != start + body_size:  # pragma: no cover - 只可能因内核 bug 触发
            raise BiliError("合并器内部偏移计算不一致", hint=f"{cursor} ≠ {start + body_size}")

        offsets = [[positions[first] for first in chunk_starts[track]] for track in range(len(sources))]
        moov = assemble(wide, offsets)
        if len(moov) != len(blob):  # pragma: no cover - 只可能因内核 bug 触发
            raise BiliError("合并器内部 moov 长度不稳定", hint=f"{len(moov)} ≠ {len(blob)}")

        dest.parent.mkdir(parents=True, exist_ok=True)
        with part.open("wb") as out:
            out.write(ftyp)
            out.write(moov)
            out.write(struct.pack(">I", body_size + 8) + b"mdat")
            written = 0
            for track, offset, length in _write_runs(layout):
                out.write(sources[track].data[offset : offset + length])
                written += length
                if progress is not None:
                    progress(written, body_size)

        if part.stat().st_size == 0:  # pragma: no cover - 仅防御
            raise BiliError("合并产物为空")
        os.replace(part, dest)
        return dest
    except OSError as exc:
        raise BiliError(f"写合并产物失败：{dest}", hint=str(exc)) from exc
    finally:
        part.unlink(missing_ok=True)
        for source in sources:
            source.close()


# --------------------------------------------------------------------------- #
# 产物探测（取代原来的 ffprobe 调用）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Stream:
    """产物里的一条流。字段刻意与 ffprobe 的输出对齐，方便沿用既有校验逻辑。"""

    kind: str  #: ``video`` / ``audio`` / ``subtitle`` / ``other``
    codec: str  #: 采样入口的四字符码，如 ``avc1`` / ``hvc1`` / ``av01`` / ``mp4a``
    timescale: int
    duration: float  #: 秒
    width: int = 0
    height: int = 0


@dataclass(frozen=True, slots=True)
class Info:
    duration: float
    streams: tuple[Stream, ...]

    @property
    def kinds(self) -> set[str]:
        return {stream.kind for stream in self.streams}


_HANDLER_KINDS = {b"vide": "video", b"soun": "audio", b"subt": "subtitle", b"text": "subtitle"}


def _mvhd_duration(mvhd: bytes) -> tuple[int, int]:
    """返回 ``(timescale, duration)``。"""
    version = mvhd[8]
    if version == 0:
        return struct.unpack_from(">II", mvhd, 20)
    return struct.unpack_from(">IQ", mvhd, 28)


def _tkhd_size(tkhd: bytes) -> tuple[int, int]:
    """``tkhd`` 里的显示宽高，16.16 定点数。音频轨通常是 0。"""
    if len(tkhd) < 92:
        return 0, 0
    base = 84 if tkhd[8] == 0 else 96
    if len(tkhd) < base + 8:
        return 0, 0
    width, height = struct.unpack_from(">II", tkhd, base)
    return width >> 16, height >> 16


def _probe_mapped(data) -> Info | None:
    found = find_box(data, [b"moov", b"mvhd"])
    if not found:
        return None
    timescale, duration = _mvhd_duration(_slice(data, found))
    moov = find_box(data, [b"moov"])
    if not moov:
        return None

    streams: list[Stream] = []
    for offset, size, kind in iter_boxes(data, moov[0] + 8, moov[0] + moov[1]):
        if kind != b"trak":
            continue
        # find_box 的 (start, end) 是要搜索的**同级区域**，不是盒子的 (偏移, 长度)：
        # 搜 trak 的子盒就得从 offset + 8 开始。这里两个坑都踩过。
        # 路径也必须写全：mdhd/hdlr/stsd 嵌在 mdia 里面，不是 trak 的直接子盒。
        trak = (offset + 8, offset + size)
        tkhd = _slice(data, find_box(data, [b"tkhd"], *trak))
        mdhd = _slice(data, find_box(data, [b"mdia", b"mdhd"], *trak))
        if not mdhd:
            continue
        hdlr = _slice(data, find_box(data, [b"mdia", b"hdlr"], *trak))
        stsd = _slice(data, find_box(data, [b"mdia", b"minf", b"stbl", b"stsd"], *trak))
        handler = hdlr[16:20] if len(hdlr) >= 20 else b""
        media_timescale = _parse_mdhd_timescale(mdhd)
        media_duration = struct.unpack_from(
            ">I" if mdhd[8] == 0 else ">Q", mdhd, 24 if mdhd[8] == 0 else 32
        )[0]
        width, height = _tkhd_size(tkhd)
        streams.append(
            Stream(
                kind=_HANDLER_KINDS.get(handler, "other"),
                codec=stsd[20:24].decode("ascii", "replace") if len(stsd) >= 24 else "",
                timescale=media_timescale,
                duration=media_duration / media_timescale if media_timescale else 0.0,
                width=width,
                height=height,
            )
        )

    if not streams:
        return None
    return Info(duration=duration / timescale if timescale else 0.0, streams=tuple(streams))


def probe(path: Path) -> Info | None:
    """读一个 MP4 的 ``moov``，返回时长与各条流；读不出来时返回 ``None``。

    返回 ``None`` 而不是抛错是刻意的，沿用了原来 ffprobe 路径的语义：**读不出来
    不等于文件坏**，调用方据此放弃校验，而不是去否定一个已经合并好的产物。
    """
    try:
        if path.stat().st_size < 8:
            return None
        with path.open("rb") as handle:
            data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                return _probe_mapped(data)
            finally:
                data.close()
    except (OSError, ValueError, struct.error, IndexError, KeyError):
        return None
