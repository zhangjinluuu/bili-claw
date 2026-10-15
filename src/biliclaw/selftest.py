"""``--check`` 用的内置样本：把合并内核真的跑一遍。

为什么值得花这 3 KB：``--check`` 是用户装完新产物后敲的第一条命令，也是唯一能在
**用户自己的机器上**发现"这个构建的合并是坏的"的地方——打包漏了字节、平台差异、
临时目录不可写，都会在这里现形，而不是等到下完 500 MB 才炸。

两段样本由 ``tests/fmp4.py`` 合成（重新生成的命令见 ``tests/test_selftest.py``）：
视频轨 ``media_time=1600``（B站 视频轨就是这样），音频轨用另一个 timescale，
保证自检覆盖到编辑列表校正与跨刻度归并这两处最容易写错的地方。
"""

from __future__ import annotations

import base64
import hashlib
import tempfile
from pathlib import Path

from . import mp4

_VIDEO_B64 = (
    "AAAAIGZ0eXBpc281AAACAGlzbzVpc282bXA0MWRhc2gAAAInbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAAAAAQAA"
    "AQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAgAAAXt0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAA"
    "AAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAoAAAAFoAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAAAAAAGQAABAAAAAADz"
    "bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAA+gAAAAABVxAAAAAAAKWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABiaWxp"
    "Y2xhdwAAAACibWluZgAAABR2bWhkAAAAAQAAAAAAAAAAAAAAGGRpbmYAAAAQZHJlZgAAAAAAAAAAAAAAbnN0YmwAAABmc3Rz"
    "ZAAAAAAAAAABAAAAVmF2YzEAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4bXZleAAAABBtZWhkAAAAAAAAAAAAAAAgdHJleAAAAAAAAAABAAAAAQAAAAAA"
    "AAAAAAAAAAAAAIhtb29mAAAAEG1maGQAAAAAAAAAAQAAAHB0cmFmAAAAHHRmaGQAAgA4AAAAAQAAAAAAAAAAAAAAAAAAABR0"
    "ZmR0AQAAAAAAAAAAAAAAAAAAOHRydW4AAA8FAAAAAgAAAJACAAAAAAACAAAAAHgAAAAAAAAAAAAAAgAAAABQAAEAAAAAAQAA"
    "AADQbWRhdAQLEhkgJy41PENKUVhfZm10e4KJkJeepayzusHIz9bd5Ovy+QUMExohKC82PURLUllgZ251fIOKkZifpq20u8LJ"
    "0Nfe5ezz+gYNFBsiKTA3PkVMU1phaG92fYSLkpmgp661vMPK0djf5u30AAcOFRwjKjE4P0ZNVFtiaXB3foWMk5qhqK+2vcTL"
    "0tng5+71AQgPFh0kKzI5QEdOVVxjanF4f4aNlJuiqbC3vsXM09rh6O/2AgkQFx4lLDM6QUhPVl1ka3J5gIeOAAAAiG1vb2YA"
    "AAAQbWZoZAAAAAAAAAABAAAAcHRyYWYAAAAcdGZoZAACADgAAAABAAAAAAAAAAAAAAAAAAAAFHRmZHQBAAAAAAAAAAAABAAA"
    "AAA4dHJ1bgAADwUAAAACAAAAkAIAAAAAAAIAAAAAQAAAAAAAAAIAAAACAAAAACgAAQAAAAADAAAAAHBtZGF0BAsSGSAnLjU8"
    "Q0pRWF9mbXR7gomQl56lrLO6wcjP1t3k6/L5BQwTGiEoLzY9REtSWWBnbnV8g4qRmJ+mrbS7wsnQ197l7PP6Bg0UGyIpMDc+"
    "RUxTWmFob3Z9hIuSmaCnrrW8w8rR2N8="
)

_AUDIO_B64 = (
    "AAAAIGZ0eXBpc281AAACAGlzbzVpc282bXA0MWRhc2gAAAInbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAAAAAQAA"
    "AQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAgAAAXt0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAA"
    "AAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAAAAAAAAAABAAAAAADz"
    "bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAACsRAAAAABVxAAAAAAAKWhkbHIAAAAAAAAAAHNvdW4AAAAAAAAAAAAAAABiaWxp"
    "Y2xhdwAAAACibWluZgAAABRzbWhkAAAAAQAAAAAAAAAAAAAAGGRpbmYAAAAQZHJlZgAAAAAAAAAAAAAAbnN0YmwAAABmc3Rz"
    "ZAAAAAAAAAABAAAAVm1wNGEAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4bXZleAAAABBtZWhkAAAAAAAAAAAAAAAgdHJleAAAAAAAAAABAAAAAQAAAAAA"
    "AAAAAAAAAAAAAIhtb29mAAAAEG1maGQAAAAAAAAAAQAAAHB0cmFmAAAAHHRmaGQAAgA4AAAAAQAAAAAAAAAAAAAAAAAAABR0"
    "ZmR0AQAAAAAAAAAAAAAAAAAAOHRydW4AAA8FAAAAAgAAAJACAAAAAAAEAAAAACgAAAAAAAAAAAAABAAAAAAgAAEAAAAAAAAA"
    "AABQbWRhdAUMExohKC82PURLUllgZ251fIOKkZifpq20u8LJ0Nfe5ezz+gYNFBsiKTA3PkVMU1phaG92fYSLkpmgp661vMPK"
    "0djf5u30AAAAAIhtb29mAAAAEG1maGQAAAAAAAAAAQAAAHB0cmFmAAAAHHRmaGQAAgA4AAAAAQAAAAAAAAAAAAAAAAAAABR0"
    "ZmR0AQAAAAAAAAAAAAgAAAAAOHRydW4AAA8FAAAAAgAAAJACAAAAAAAEAAAAADAAAAAAAAAAAAAABAAAAAAcAAEAAAAAAAAA"
    "AABUbWRhdAUMExohKC82PURLUllgZ251fIOKkZifpq20u8LJ0Nfe5ezz+gYNFBsiKTA3PkVMU1phaG92fYSLkpmgp661vMPK"
    "0djf5u30AAcOFRw="
)


#: 期望的样本字节摘要：400 字节视频 + 148 字节音频，按合并后的顺序串起来。
#: 钉住摘要是这个自检的真正价值——只断言"产物存在"会让半损坏的构建蒙混过关。
_EXPECTED_DIGEST = "f3b4418049be1e43bbcf30a328c90a5c71c396c5f59577c16ffe1bd08ed454cc"

#: 产物里应当出现的样本数据总字节数（120+80+64+40 视频，40+32+48+28 音频）。
_EXPECTED_PAYLOAD = 452


def check_merge() -> str | None:
    """现场合并内置样本，验证 MP4 内核可用。

    返回 ``None`` 表示健康，否则返回一句可以直接打给用户看的失败原因。
    这里**不抛异常**：``--check`` 的职责是报告，不是中断。
    """
    try:
        with tempfile.TemporaryDirectory(prefix="biliclaw-selftest-") as workdir:
            root = Path(workdir)
            video = root / "v.m4s"
            audio = root / "a.m4s"
            video.write_bytes(base64.b64decode(_VIDEO_B64))
            audio.write_bytes(base64.b64decode(_AUDIO_B64))
            dest = mp4.merge(video=video, audio=audio, dest=root / "out.mp4")

            info = mp4.probe(dest)
            if info is None:
                return "合并产物读不回来（probe 返回 None）"
            kinds = sorted(stream.kind for stream in info.streams)
            if kinds != ["audio", "video"]:
                return f"合并产物只有 {kinds or '零'} 条轨道，应当是 audio + video"
            if info.duration <= 0:
                return "合并产物的时长为 0"

            payload = _payload_of(dest.read_bytes())
            if payload is None:
                return "合并产物里找不到 mdat 数据"
            if len(payload) != _EXPECTED_PAYLOAD:
                return f"合并产物含 {len(payload)} 字节样本数据，应当是 {_EXPECTED_PAYLOAD} 字节"
            digest = hashlib.sha256(payload).hexdigest()
            if digest != _EXPECTED_DIGEST:
                return "合并产物的样本数据与预期不符（内容被改动了）"
    except Exception as error:  # noqa: BLE001 —— 自检要吞掉一切，把原因如实报上去
        return f"{type(error).__name__}: {error}"
    return None


def _payload_of(blob: bytes) -> bytes | None:
    """按顶层盒子走一遍，把 ``mdat`` 正文拼起来。"""
    chunks: list[bytes] = []
    offset = 0
    while offset + 8 <= len(blob):
        size = int.from_bytes(blob[offset : offset + 4], "big")
        kind = blob[offset + 4 : offset + 8]
        header = 8
        if size == 1:
            size = int.from_bytes(blob[offset + 8 : offset + 16], "big")
            header = 16
        elif size == 0:
            size = len(blob) - offset
        if size < header or offset + size > len(blob):
            return None
        if kind == b"mdat":
            chunks.append(blob[offset + header : offset + size])
        offset += size
    return b"".join(chunks) if chunks else None
