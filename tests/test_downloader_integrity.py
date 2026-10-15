"""分片下载的完整性防线。

这里的每一项都对应一个**真实存在过的缺陷**——共同点是会把损坏的文件当成成功交出去，
而且退出码是 0。这类缺陷比崩溃危险得多：崩溃会被立刻发现，静默损坏要等到用户
把文件播到一半、或者拿去投稿被驳回时才知道。

假 CDN 用 ``httpx.MockTransport`` 造，因为要复现的坏行为（多吐一截、区间标签说谎、
传到一半断连接）真实 CDN 不会配合演出。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from biliclaw.downloader import StreamDownloader
from biliclaw.errors import BiliError, ExitCode, FileSystemError, NetworkError

#: 下载器允许的最小分片。用它来把测试负载压到几百 KiB，跑得快。
CHUNK = 64 * 1024
SIZE = 4 * CHUNK


def make_payload(seed: int = 0, size: int = SIZE) -> bytes:
    """可复现的伪随机负载。

    刻意不用纯填充字节：要能分辨"下错了流"和"下对了流"，两份负载必须真的不同。
    """
    out = bytearray()
    value = seed * 2654435761 + 1
    while len(out) < size:
        value = (value * 1103515245 + 12345) & 0xFFFFFFFF
        out += value.to_bytes(4, "little")
    return bytes(out[:size])


def _parse_range(value: str) -> tuple[int, int]:
    start, _, end = value.removeprefix("bytes=").partition("-")
    return int(start), int(end)


class _TruncatedStream(httpx.AsyncByteStream):
    """给一半就断掉，模拟 CDN 中途关连接——httpx 会因此抛 ``RemoteProtocolError``。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._payload[: len(self._payload) // 2]
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )

    async def aclose(self) -> None:
        return None


class FakeCDN:
    """一个行为可控的假 CDN。每种坏行为都默认关闭，测试里按需打开。"""

    def __init__(
        self,
        payload: bytes,
        *,
        supports_range: bool = True,
        overlong_once: Iterable[int] = (),
        overlong_always: Iterable[int] = (),
        lying_content_range: bool = False,
        truncate_whole: bool = False,
    ) -> None:
        self.payload = payload
        self.supports_range = supports_range
        self.overlong_once = set(overlong_once)
        self.overlong_always = set(overlong_always)
        self.lying_content_range = lying_content_range
        self.truncate_whole = truncate_whole
        #: 记录每一次真正的分片请求（不含探测请求），便于断言重试与续传行为
        self.calls: list[tuple[int, int]] = []
        self._already_overlong: set[int] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        header = request.headers.get("Range")
        if not self.supports_range or header is None:
            return self._whole()

        start, end = _parse_range(header)
        if (start, end) == (0, 0):
            # 探测请求：只回答"支持 Range"和"总长多少"
            return httpx.Response(
                206,
                headers={"Content-Range": f"bytes 0-0/{len(self.payload)}"},
                content=self.payload[:1],
            )
        return self._chunk(start, end)

    def _whole(self) -> httpx.Response:
        headers = {"Content-Type": "application/octet-stream"}
        if self.truncate_whole:
            headers["Content-Length"] = str(len(self.payload))
            return httpx.Response(200, headers=headers, stream=_TruncatedStream(self.payload))
        return httpx.Response(200, headers=headers, content=self.payload)

    def _chunk(self, start: int, end: int) -> httpx.Response:
        self.calls.append((start, end))
        length = end - start + 1

        headers = {"Content-Range": f"bytes {start}-{end}/{len(self.payload)}"}
        if self.lying_content_range and start:
            # 内容是对的，区间标签却在说谎——代理缓存串了响应时常是这样。
            # 相邻分片拿到同一条响应时，只看长度是发现不了的。
            headers = {"Content-Range": f"bytes 0-{length - 1}/{len(self.payload)}"}

        if start in self.overlong_always:
            return httpx.Response(206, headers=headers, content=self.payload[start : start + length + 1024])
        if start in self.overlong_once and start not in self._already_overlong:
            self._already_overlong.add(start)
            return httpx.Response(206, headers=headers, content=self.payload[start : start + length + 1024])

        return httpx.Response(206, headers=headers, content=self.payload[start : end + 1])


@asynccontextmanager
async def _downloader(cdn: FakeCDN, **kwargs: object) -> AsyncIterator[StreamDownloader]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(cdn)) as client:
        yield StreamDownloader(client, chunk_size=CHUNK, **kwargs)


URL = "https://cdn.example/v.m4s"


# --------------------------------------------------------------------------- #
# 分片越界：会写坏相邻分片的那一类
# --------------------------------------------------------------------------- #


async def test_overlong_chunk_response_is_rejected(tmp_path) -> None:
    """返回数据超出请求范围的分片必须被拒绝。

    旧的实现直接 ``handle.seek(chunk.start)`` 然后一路写下去，多出来的字节会覆盖
    **相邻分片**的地盘；而相邻分片此时可能早已完成、不会被重下，于是产出一个
    前半段正确、中间一段错位、却报告成功的文件。
    """
    payload = make_payload()
    cdn = FakeCDN(payload, overlong_always={CHUNK})
    dest = tmp_path / "v.m4s"

    async with _downloader(cdn, concurrency=4, retries=1) as downloader:
        with pytest.raises(NetworkError):
            await downloader.download([URL], dest)

    assert not dest.exists(), "失败时不能产出文件"
    assert (tmp_path / "v.m4s.part").exists(), "分片要留着供续传"


async def test_overlong_chunk_response_recovers_on_retry(tmp_path) -> None:
    """偶尔多吐一截是瞬态故障，重试就该拿到正确的文件——而不是留下一个坏文件。"""
    payload = make_payload()
    cdn = FakeCDN(payload, overlong_once={CHUNK})
    dest = tmp_path / "v.m4s"

    async with _downloader(cdn, concurrency=4, retries=2) as downloader:
        await downloader.download([URL], dest)

    assert dest.read_bytes() == payload, "重试后的文件与源数据不一致"


async def test_lying_content_range_is_rejected(tmp_path) -> None:
    """区间标签与请求不符时要报错——这正是代理返回了错位缓存的特征。"""
    payload = make_payload()
    cdn = FakeCDN(payload, lying_content_range=True)
    dest = tmp_path / "v.m4s"

    async with _downloader(cdn, concurrency=4, retries=1) as downloader:
        with pytest.raises(NetworkError):
            await downloader.download([URL], dest)

    assert not dest.exists()


# --------------------------------------------------------------------------- #
# 断点元数据必须绑定流的身份
# --------------------------------------------------------------------------- #


def _seed_stale_part(dest, payload: bytes, identity: str | None) -> None:
    """伪造一份"看起来完整"的上次残留：长度对得上，分片号也说全下完了。"""
    part = dest.parent / f"{dest.name}.part"
    sidecar = dest.parent / f"{dest.name}.part.json"
    part.write_bytes(payload)
    sidecar.write_text(
        f'{{"total": {len(payload)}, "chunk_size": {CHUNK}, '
        f'"identity": {identity!r}, "done": [0, 1, 2, 3]}}'.replace("'", '"'),
        encoding="utf-8",
    )


async def test_stale_part_from_a_different_stream_is_not_reused(tmp_path) -> None:
    """换清晰度重跑时，残留的 .part 长度恰好也对得上——但内容不是这条流的。

    ``.part`` 是预分配成完整长度的，所以长度校验必然放它过去；旧实现于是直接把
    上一条流的前半段当成这条流的，产出一个混了两条流的文件并报告成功。
    """
    stale = make_payload(seed=1)
    wanted = make_payload(seed=2)
    dest = tmp_path / "v.m4s"
    _seed_stale_part(dest, stale, identity="cid=1&qn=80&codecid=7")

    cdn = FakeCDN(wanted)
    async with _downloader(cdn) as downloader:
        await downloader.download([URL], dest, identity="cid=1&qn=112&codecid=7")

    assert dest.read_bytes() == wanted, "复用了另一条流的残留分片"
    assert cdn.calls, "身份不符时必须真的重新下载"


async def test_resume_state_is_reused_when_the_identity_matches(tmp_path) -> None:
    """身份一致时续传必须照常工作——上面那条修复不能把 E2E-6 一起打掉。"""
    payload = make_payload()
    dest = tmp_path / "v.m4s"
    _seed_stale_part(dest, payload, identity="cid=1&qn=80&codecid=7")

    cdn = FakeCDN(payload)
    async with _downloader(cdn) as downloader:
        await downloader.download([URL], dest, identity="cid=1&qn=80&codecid=7")

    assert dest.read_bytes() == payload
    assert cdn.calls == [], "全部分片都已完成，不该再发分片请求"


# --------------------------------------------------------------------------- #
# 不支持 Range 的回退路径
# --------------------------------------------------------------------------- #


async def test_truncated_body_on_the_no_range_path_is_a_network_error(tmp_path) -> None:
    """整段下载传到一半断开时，用户该看到退出码 3 和一句中文，而不是 Python 堆栈。"""
    payload = make_payload()
    cdn = FakeCDN(payload, supports_range=False, truncate_whole=True)
    dest = tmp_path / "v.m4s"

    async with _downloader(cdn) as downloader:
        with pytest.raises(NetworkError) as info:
            await downloader.download([URL], dest)

    assert info.value.exit_code is ExitCode.NETWORK
    assert not dest.exists()


# --------------------------------------------------------------------------- #
# 文件系统故障（退出码 7）
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(os.geteuid() == 0, reason="root 会无视目录权限，这个测试没有意义")
async def test_unwritable_output_directory_is_a_filesystem_error(tmp_path) -> None:
    """磁盘满或没有写权限必须是退出码 7，而不是让 OSError 直接冒到用户面前。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)  # r-x：能进去，但不能在里面创建文件

    try:
        cdn = FakeCDN(make_payload())
        async with _downloader(cdn) as downloader:
            with pytest.raises(FileSystemError) as info:
                await downloader.download([URL], blocked / "v.m4s")
        assert info.value.exit_code is ExitCode.FILESYSTEM
    finally:
        blocked.chmod(0o700)


async def test_failed_whole_body_download_leaves_no_litter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """整段下载没有 sidecar，半个文件续不了传（下次从头覆盖写），只是白占磁盘。"""
    payload = make_payload(1, SIZE)

    class _Dies(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield payload[: 2 * CHUNK]
            raise httpx.ReadError("连接断了")

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        # 不支持 Range，逼下载器走整段路径
        return httpx.Response(200, headers={"Content-Length": str(SIZE)}, stream=_Dies())

    async with _downloader(handler) as downloader:
        with pytest.raises(BiliError):
            await downloader.download(["https://cdn.example/x"], tmp_path / "out.m4s")

    assert list(tmp_path.glob("*.part*")) == []
