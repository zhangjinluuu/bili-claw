"""合成 fMP4 分片，供 ``mp4`` / ``muxer`` 的测试使用。

真的去下载一段 B站分片会让测试依赖网络；用 ffmpeg 生成又会让测试依赖 ffmpeg——
而这次改动的全部意义就是去掉这个依赖。所以这里按 ISO/IEC 14496-12 手工拼出最小
可解析的分片：结构完整到足以走通解析器（``ftyp`` / ``moov`` / ``moof`` / ``mdat``
齐全），样本数据是可校验的确定性字节。

只求"解析器认得"，不求"播放器能放"——所以 ``stbl`` 里只放 ``stsd``，因为
``mp4.parse`` 本来就只从源文件读 ``stsd``，逐样本信息全部来自 ``moof``。
能播的产物由真机集成测试负责，见 ``tests/test_mp4.py`` 末尾。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

BOX = 8


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + BOX) + kind + payload


def full(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


@dataclass(frozen=True, slots=True)
class Fragment:
    """一个合成分片，外加测试断言需要的期望值。"""

    blob: bytes
    #: ``(文件偏移, 长度, cto, duration)``，顺序与样本顺序一致
    samples: list[tuple[int, int, int, int]]

    def write(self, path: Path) -> Fragment:
        path.write_bytes(self.blob)
        return self


def _mvhd(*, timescale: int, next_track_id: int) -> bytes:
    return full(
        b"mvhd",
        0,
        0,
        struct.pack(">II", 0, 0)  # creation / modification
        + struct.pack(">II", timescale, 0)  # timescale / duration（分片源里是 0）
        + struct.pack(">ihH", 0x00010000, 0x0100, 0)  # rate / volume / reserved
        + b"\x00" * 8
        + struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
        + b"\x00" * 24
        + struct.pack(">I", next_track_id),
    )


def _tkhd(*, track_id: int, width: int, height: int) -> bytes:
    return full(
        b"tkhd",
        0,
        0x000003,  # enabled | in_movie
        struct.pack(">II", 0, 0)
        + struct.pack(">II", track_id, 0)  # track_id / reserved
        + struct.pack(">I", 0)  # duration（分片源里是 0）
        + b"\x00" * 8
        + struct.pack(">hhhh", 0, 0, 0, 0)  # layer / alt_group / volume / reserved
        + struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
        + struct.pack(">II", width << 16, height << 16),
    )


def _elst(media_time: int) -> bytes:
    """编辑列表。``media_time`` 非零时整条轨道的显示时间轴会平移——B站视频轨就是 1600。"""
    return box(b"edts", full(b"elst", 0, 0, struct.pack(">IiiHH", 1, 0, media_time, 1, 0)))


def _mdhd(timescale: int) -> bytes:
    return full(
        b"mdhd",
        0,
        0,
        struct.pack(">II", 0, 0) + struct.pack(">II", timescale, 0) + struct.pack(">HH", 0x55C4, 0),
    )


def _hdlr(handler: bytes) -> bytes:
    return full(b"hdlr", 0, 0, b"\x00" * 4 + handler + b"\x00" * 12 + b"biliclaw\x00")


def _stsd(codec: bytes) -> bytes:
    # 采样入口：6 字节保留 + data_reference_index，再补足到 VisualSampleEntry 的长度。
    # 解析器只读类型字段，内容不参与解析。
    entry = box(codec, b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 70)
    return full(b"stsd", 0, 0, struct.pack(">I", 1) + entry)


def _trex(track_id: int, duration: int, size: int, flags: int) -> bytes:
    return full(b"trex", 0, 0, struct.pack(">IIIII", track_id, 1, duration, size, flags))


def build(
    *,
    handler: bytes = b"vide",
    codec: bytes = b"avc1",
    timescale: int = 16000,
    sizes: list[int] | None = None,
    durations: list[int] | None = None,
    ctos: list[int] | None = None,
    media_time: int = 0,
    width: int = 640,
    height: int = 360,
    per_fragment: int | None = None,
    seed: int = 0,
    brands: tuple[bytes, ...] = (b"iso5", b"iso6", b"mp41", b"dash"),
) -> Fragment:
    """拼一个分片。

    ``per_fragment`` 控制每个 ``moof`` 里装几个样本；``None`` 表示全部装进一个，
    用来复现"单片段多 trun 条目"和"多片段"两种真实排布。``seed`` 让不同轨的样本
    数据彼此可区分，测试才能验证搬运没有串轨。
    """
    sizes = sizes if sizes is not None else [100, 50, 70]
    durations = durations if durations is not None else [512] * len(sizes)
    ctos = ctos if ctos is not None else [0] * len(sizes)
    count = len(sizes)

    ftyp = (
        struct.pack(">I", 16 + 4 * len(brands))
        + b"ftyp"
        + brands[0]
        + struct.pack(">I", 0x200)
        + b"".join(brands)
    )

    stbl = box(b"stbl", _stsd(codec))
    minf = box(
        b"minf",
        full(b"vmhd" if handler == b"vide" else b"smhd", 0, 1, b"\x00" * 8)
        + box(b"dinf", full(b"dref", 0, 0, struct.pack(">I", 0)))
        + stbl,
    )
    trak = box(
        b"trak",
        _tkhd(track_id=1, width=width, height=height)
        + _elst(media_time)
        + box(b"mdia", _mdhd(timescale) + _hdlr(handler) + minf),
    )
    moov = box(
        b"moov",
        _mvhd(timescale=1000, next_track_id=2)
        + trak
        + box(b"mvex", full(b"mehd", 0, 0, struct.pack(">I", 0)) + _trex(1, 0, 0, 0)),
    )

    sizes_of = sizes
    body = bytearray()
    expected: list[tuple[int, int, int, int]] = []

    step = per_fragment or count
    for first in range(0, count, step):
        chunk = list(range(first, min(first + step, count)))
        decode_time = sum(durations[:first])

        tfhd = full(
            b"tfhd",
            0,
            0x020038,  # default-base-is-moof | default duration/size/flags
            struct.pack(">IIII", 1, 0, 0, 0),
        )
        tfdt = full(b"tfdt", 1, 0, struct.pack(">Q", decode_time))

        # 默认参数把循环变量钉进闭包：make_trun 在同一次迭代里被调用两次，
        # 若直接引用 chunk/first，读代码的人（和 ruff B023）都得先确认这一点。
        def make_trun(data_offset: int, chunk=chunk, first=first) -> bytes:
            entries = b"".join(
                struct.pack(">IIIi", durations[i], sizes_of[i], _flags(i == first), ctos[i])
                for i in chunk
            )
            payload = struct.pack(">IiI", len(chunk), data_offset, 0x02000000) + entries
            # 0x000F05 = data-offset | first-sample-flags | duration | size | sample-flags | cto
            # 六个位必须与**实际写进去的字段**一一对应：少声明一位，解析器就会按更短的
            # 步长走，从第二个样本起全部错位（真实 B站 分片是 0x000B05，不带 sample-flags）。
            return full(b"trun", 0, 0x000F05, payload)

        # data_offset 指向 moof 之后紧邻的 mdat 正文，而它自己的长度又取决于 trun
        # 的大小——所以先用占位值算 moof 长度，再重建一次。这与 mp4.merge 里
        # "先占位求长度、再回填偏移"是同一个套路。
        placeholder = make_trun(0)
        header = full(b"mfhd", 0, 0, struct.pack(">I", 1))
        moof_size = len(box(b"moof", header + box(b"traf", tfhd + tfdt + placeholder)))
        moof = box(b"moof", header + box(b"traf", tfhd + tfdt + make_trun(moof_size + BOX)))
        assert len(moof) == moof_size

        mdat_start = len(ftyp) + len(moov) + len(body) + len(moof) + BOX
        payload_start = mdat_start
        for sample in chunk:
            expected.append((payload_start, sizes_of[sample], ctos[sample], durations[sample]))
            payload_start += sizes_of[sample]
        total = sum(sizes_of[sample] for sample in chunk)
        body += moof + box(b"mdat", bytes([(i * 7 + 3 + seed) % 251 for i in range(total)]))

    return Fragment(blob=ftyp + moov + bytes(body), samples=expected)


def _flags(sync: bool) -> int:
    """``sample_flags``：bit16 是 ``sample_is_non_sync_sample``。"""
    return 0 if sync else 0x00010000
