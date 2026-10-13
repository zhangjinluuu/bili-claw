"""分片并发下载、断点续传与换链重试（FR-04）。

设计要点：

* **流式写盘**——数据一边从网络读进来一边写到文件偏移处，任何时刻内存里只有一个块，
  与文件大小无关（FR-04.7）。
* **断点续传**——下载中的文件叫 ``xxx.part``，旁边有一个 ``xxx.part.json``
  记录总长度、分片大小与已完成的分片号。进程被杀掉后重跑同一命令，
  校验长度一致就只补缺失的分片（FR-04.2）。元数据里还记着这条流的身份
  （``identity``：cid+qn+codecid），换清晰度或换编码重跑时宁可重下，
  也不能把上一条流的前半段当成这条流的（FR-04.6）。
* **换链**——CDN 链接约 2 小时过期，过期时返回 403。这里把它翻译成
  ``LinkExpiredError`` 抛给上层，由上层重新请求 playurl 拿到新地址后
  带着同一份 ``.part`` 继续（FR-04.3）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from .errors import BiliError, FileSystemError, LinkExpiredError, NetworkError

log = logging.getLogger("bilidl.downloader")

#: 单个分片的目标大小。太小会放大请求开销，太大则并发调度不够灵活。
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_CONCURRENCY = 4

_READ_BLOCK = 256 * 1024

ProgressCallback = Callable[[int], None]


# --------------------------------------------------------------------------- #
# 分片规划
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    start: int
    end: int  # 闭区间

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def range_header(self) -> str:
        return f"bytes={self.start}-{self.end}"


def plan_chunks(total: int, chunk_size: int) -> list[Chunk]:
    """把 ``[0, total)`` 切成等长分片，最后一片可能更短。"""
    if total <= 0:
        return []
    size = max(1, chunk_size)
    chunks: list[Chunk] = []
    index = 0
    start = 0
    while start < total:
        end = min(start + size, total) - 1
        chunks.append(Chunk(index=index, start=start, end=end))
        index += 1
        start = end + 1
    return chunks


def parse_rate(text: str | None) -> float | None:
    """把 ``"1M"`` / ``"500K"`` / ``"800000"`` 解析成字节/秒。"""
    if not text:
        return None
    cleaned = text.strip().lower().replace("/s", "").replace("bps", "").strip()
    multipliers = {"k": 1024, "m": 1024**2, "g": 1024**3}
    multiplier = 1
    if cleaned and cleaned[-1] in multipliers:
        multiplier = multipliers[cleaned[-1]]
        cleaned = cleaned[:-1]
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value * multiplier if value > 0 else None


class _RateLimiter:
    """跨分片共享的令牌桶限速器。"""

    def __init__(self, bytes_per_second: float, burst: int = _READ_BLOCK) -> None:
        self._rate = max(1.0, bytes_per_second)
        self._capacity = max(float(burst), self._rate)
        self._allowance = self._capacity
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def consume(self, amount: int) -> None:
        async with self._lock:
            now = time.monotonic()
            self._allowance = min(self._capacity, self._allowance + (now - self._updated_at) * self._rate)
            self._updated_at = now
            if amount > self._allowance:
                await asyncio.sleep((amount - self._allowance) / self._rate)
                self._allowance = 0.0
                self._updated_at = time.monotonic()
            else:
                self._allowance -= amount


# --------------------------------------------------------------------------- #
# 断点元数据
# --------------------------------------------------------------------------- #


@dataclass
class _ResumeState:
    total: int
    chunk_size: int
    done: set[int]


def _part_paths(dest: Path) -> tuple[Path, Path]:
    """返回 ``(临时文件, 断点元数据)``。用追加后缀而不是替换后缀，
    这样 ``a.mp4`` 对应的是 ``a.mp4.part``，一眼能看出它属于谁。"""
    return dest.parent / f"{dest.name}.part", dest.parent / f"{dest.name}.part.json"


# --------------------------------------------------------------------------- #
# 下载器
# --------------------------------------------------------------------------- #


class StreamDownloader:
    """下载单条流（视频轨或音频轨）。同一个实例可复用于多个文件。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        retries: int = 3,
        limit_rate: float | None = None,
    ) -> None:
        self._client = client
        self._concurrency = max(1, concurrency)
        self._chunk_size = max(64 * 1024, chunk_size)
        self._retries = max(0, retries)
        self._limiter = _RateLimiter(limit_rate) if limit_rate else None

    async def download(
        self,
        urls: Sequence[str],
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
        on_total: ProgressCallback | None = None,
        total_hint: int | None = None,
        identity: str | None = None,
    ) -> Path:
        """把 ``urls`` 指向的流下载到 ``dest``（原子完成：先在 .part 上写，最后改名）。

        ``on_total`` 在探测到真实长度后回调一次，供进度条校正总数
        （接口给的 ``size`` 字段并非总是存在）。

        ``identity`` 标识这条流的身份（cid+qn+codecid）。断点元数据会把它一起
        记下来，重跑时若身份对不上就当作全新下载——否则换了清晰度重跑，
        上一条流已经下好的前半段会被当成这条流的，产出一个前半段来自 480P、
        后半段来自 1080P 的文件并且报告成功。
        """
        if not urls:
            raise NetworkError("没有可用的下载地址")

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _filesystem_error(exc, "创建输出目录", dest.parent) from exc
        part, sidecar = _part_paths(dest)

        url, total, supports_range = await self._probe(urls, total_hint)
        log.debug("探测结果：总长 %s，支持 Range=%s", total, supports_range)

        if on_total and total > 0:
            on_total(total)

        if not supports_range or total <= 0:
            await self._download_whole(url, part, total, on_progress)
        else:
            await self._download_chunked(urls, part, sidecar, total, on_progress, identity)

        # 收尾前再确认一次长度：分片逻辑已经各自校验过，但这里是最后一道闸门，
        # 宁可重下也不能把长度不对的文件改名成"成功"。
        try:
            actual = part.stat().st_size
        except OSError as exc:
            raise _filesystem_error(exc, "读取临时文件", part) from exc
        if total > 0 and actual != total:
            raise NetworkError(
                f"下载结果长度不符：期望 {total} 字节，实际 {actual}",
                hint="重跑会重新下载；若反复出现请加 --debug 反馈",
            )

        try:
            part.replace(dest)
        except OSError as exc:
            raise _filesystem_error(exc, "写入输出文件", dest) from exc
        sidecar.unlink(missing_ok=True)
        return dest

    # -- 探测 -------------------------------------------------------------- #

    async def _probe(self, urls: Sequence[str], total_hint: int | None) -> tuple[str, int, bool]:
        """确认哪条链接可用、总长度多少、是否支持 Range。"""
        expired = False
        last_error: BiliError | None = None

        for url in urls:
            try:
                request = self._client.build_request("GET", url, headers={"Range": "bytes=0-0"})
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                last_error = NetworkError(f"连接 CDN 失败：{exc.__class__.__name__}")
                continue

            try:
                if response.status_code == 206:
                    total = _total_from_content_range(response.headers.get("content-range"))
                    if total:
                        if total_hint and total_hint != total:
                            log.debug(
                                "接口声明的长度 %d 与 CDN 的 %d 不一致，以 CDN 为准", total_hint, total
                            )
                        return url, total, True
                    if total_hint:
                        # CDN 没给可解析的总长时退回到接口声明的长度，至少还能校验字节数
                        log.debug("Content-Range 无法解析，改用接口声明的长度 %d", total_hint)
                        return url, total_hint, True
                    last_error = NetworkError("CDN 返回的 Content-Range 无法解析")
                elif response.status_code == 200:
                    length = response.headers.get("content-length")
                    if length and int(length) > 0:
                        return url, int(length), False
                    if total_hint:
                        log.debug("CDN 未返回长度，改用接口声明的长度 %d", total_hint)
                        return url, total_hint, False
                    last_error = NetworkError("CDN 未返回文件长度")
                elif response.status_code == 403:
                    expired = True
                else:
                    last_error = NetworkError(f"CDN 返回 HTTP {response.status_code}")
            finally:
                await response.aclose()

        if expired:
            raise LinkExpiredError(
                "CDN 链接已失效（HTTP 403）",
                hint="播放地址有效期约 2 小时，将自动重新获取",
            )
        raise last_error or NetworkError("所有候选地址都不可用")

    # -- 整段下载（不支持 Range 时的回退路径） ------------------------------ #

    async def _download_whole(
        self, url: str, part: Path, total: int, on_progress: ProgressCallback | None
    ) -> None:
        written = 0
        try:
            async with self._client.stream("GET", url) as response:
                if response.status_code == 403:
                    raise LinkExpiredError("CDN 链接已失效（HTTP 403）", hint="将自动重新获取播放地址")
                if response.status_code != 200:
                    raise NetworkError(f"下载失败：HTTP {response.status_code}")

                try:
                    handle = open(part, "wb")
                except OSError as exc:
                    raise _filesystem_error(exc, "写入临时文件", part) from exc

                with handle:
                    async for block in response.aiter_bytes(_READ_BLOCK):
                        try:
                            handle.write(block)
                        except OSError as exc:
                            raise _filesystem_error(exc, "写入", part) from exc
                        written += len(block)
                        if on_progress:
                            on_progress(len(block))
                        if self._limiter:
                            await self._limiter.consume(len(block))
        except httpx.TransportError as exc:
            # 连接中途断掉时 httpx 抛的是它自己的异常。不翻译的话用户看到的是一段
            # Python 堆栈，而 FR-11 要求这种情况退出码 3、FR-12 要求不出无关堆栈。
            raise NetworkError(
                f"下载中断：{exc.__class__.__name__}",
                hint="网络不稳定，重跑会从断点继续",
            ) from exc

        if total > 0 and written != total:
            raise NetworkError(
                f"下载不完整：期望 {total} 字节，实际 {written}",
                hint="重跑会从断点继续",
            )

    # -- 分片下载 ---------------------------------------------------------- #

    async def _download_chunked(
        self,
        urls: Sequence[str],
        part: Path,
        sidecar: Path,
        total: int,
        on_progress: ProgressCallback | None,
        identity: str | None = None,
    ) -> None:
        state = _load_state(sidecar, part, total, self._chunk_size, identity)
        chunks = plan_chunks(total, self._chunk_size)

        if state is None:
            # 全新下载：预分配成稀疏文件，各分片直接 seek 写入
            try:
                with open(part, "wb") as handle:
                    handle.truncate(total)
            except OSError as exc:
                raise _filesystem_error(exc, "预分配临时文件", part) from exc
            done: set[int] = set()
        else:
            done = state.done
            log.debug("断点续传：%d/%d 分片已完成", len(done), len(chunks))

        pending = [chunk for chunk in chunks if chunk.index not in done]
        if not pending:
            return

        semaphore = asyncio.Semaphore(self._concurrency)

        async def run(chunk: Chunk) -> None:
            async with semaphore:
                await self._fetch_chunk(urls, chunk, part, on_progress)
            done.add(chunk.index)
            _save_state(sidecar, total, self._chunk_size, done, identity)

        try:
            async with asyncio.TaskGroup() as group:
                for chunk in pending:
                    group.create_task(run(chunk))
        except BaseExceptionGroup as group:
            # 保留 .part 与元数据，下次运行从断点继续
            raise _primary_error(group) from group

    async def _fetch_chunk(
        self,
        urls: Sequence[str],
        chunk: Chunk,
        part: Path,
        on_progress: ProgressCallback | None,
    ) -> None:
        """下载一个分片，按「换个链接 → 退避重试」的顺序尝试。"""
        last_error: Exception | None = None

        for attempt in range(self._retries + 1):
            url = urls[attempt % len(urls)]
            if attempt:
                await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 8.0))
                log.debug("分片 %d 第 %d 次重试", chunk.index, attempt)

            try:
                await self._fetch_chunk_once(url, chunk, part, on_progress)
                return
            except LinkExpiredError:
                # 所有链接都过期了，重试没有意义，交给上层换链
                raise
            except (httpx.TransportError, NetworkError) as exc:
                last_error = exc
                log.debug("分片 %d 失败：%s", chunk.index, exc)

        raise NetworkError(
            f"分片 {chunk.index} 重试 {self._retries} 次后仍失败",
            hint=str(last_error) if last_error else None,
        )

    async def _fetch_chunk_once(
        self,
        url: str,
        chunk: Chunk,
        part: Path,
        on_progress: ProgressCallback | None,
    ) -> None:
        headers = {"Range": chunk.range_header()}
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code == 403:
                raise LinkExpiredError("CDN 链接已失效（HTTP 403）", hint="将自动重新获取播放地址")
            if response.status_code != 206:
                # 走到这里说明探测阶段报告支持 Range，实际却不支持——宁可报错也不能写坏文件
                raise NetworkError(f"分片请求未返回 206（HTTP {response.status_code}）")

            # Range 请求回错区间时，响应体看着一切正常、内容却是别处的数据，
            # 只靠长度校验发现不了。所以核对区间本身。
            bounds = _range_bounds(response.headers.get("content-range"))
            if bounds is not None and bounds != (chunk.start, chunk.end):
                raise NetworkError(
                    f"分片 {chunk.index} 返回的区间不对：请求 {chunk.start}-{chunk.end}，"
                    f"实际 {bounds[0]}-{bounds[1]}"
                )

            written = 0
            try:
                handle = open(part, "r+b")
            except OSError as exc:
                raise _filesystem_error(exc, "写入临时文件", part) from exc

            with handle:
                handle.seek(chunk.start)
                async for block in response.aiter_bytes(_READ_BLOCK):
                    # 超出请求范围的字节会顺着文件偏移写进**相邻分片**的地盘，而相邻
                    # 分片可能早已完成、不会重下——那就是一个静默损坏却报告成功的文件。
                    # 在这里就截住：宁可让这个分片重试，也不多写一个字节。
                    remaining = chunk.length - written
                    if len(block) > remaining:
                        raise NetworkError(
                            f"分片 {chunk.index} 返回的数据超出请求范围："
                            f"期望 {chunk.length} 字节，实际至少 {written + len(block)} 字节"
                        )
                    try:
                        handle.write(block)
                    except OSError as exc:
                        raise _filesystem_error(exc, "写入", part) from exc
                    written += len(block)
                    if on_progress:
                        on_progress(len(block))
                    if self._limiter:
                        await self._limiter.consume(len(block))

            if written != chunk.length:
                raise NetworkError(f"分片 {chunk.index} 长度不符：期望 {chunk.length}，实际 {written}")


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _total_from_content_range(header: str | None) -> int | None:
    """``bytes 0-0/1234567`` → ``1234567``。"""
    if not header or "/" not in header:
        return None
    tail = header.rsplit("/", 1)[-1].strip()
    return int(tail) if tail.isdigit() else None


def _range_bounds(header: str | None) -> tuple[int, int] | None:
    """``bytes 0-1023/4096`` → ``(0, 1023)``。解析不出来就返回 None。"""
    if not header or not header.startswith("bytes "):
        return None
    spec = header[len("bytes ") :].split("/", 1)[0].strip()
    start, sep, end = spec.partition("-")
    if not sep or not start.strip().isdigit() or not end.strip().isdigit():
        return None
    return int(start), int(end)


def _filesystem_error(exc: OSError, action: str, path: Path) -> FileSystemError:
    """把 ``OSError`` 翻译成退出码 7，并附上人能读懂的下一步。"""
    reason = exc.strerror or str(exc)
    return FileSystemError(
        f"{action} {path} 失败：{reason}",
        hint="检查磁盘剩余空间与目录读写权限",
    )


def _load_state(
    sidecar: Path,
    part: Path,
    total: int,
    chunk_size: int,
    identity: str | None = None,
) -> _ResumeState | None:
    """读取断点元数据；只要有任何不一致就当作全新下载，绝不冒险复用半个文件。

    ``identity`` 是这条流的身份。长度对得上并不代表内容对得上——换成 1080P 重跑时，
    上一条 480P 留下的 ``.part`` 早已被预分配成完整长度，长度校验会放它过去。
    """
    if not part.exists() or part.stat().st_size != total:
        return None
    if not sidecar.exists():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if payload.get("total") != total or payload.get("chunk_size") != chunk_size:
            return None
        if payload.get("identity") != identity:
            return None
        done = {int(index) for index in payload.get("done", [])}
    except (OSError, ValueError, TypeError):
        return None

    valid = {chunk.index for chunk in plan_chunks(total, chunk_size)}
    return _ResumeState(total=total, chunk_size=chunk_size, done=done & valid)


def _save_state(
    sidecar: Path,
    total: int,
    chunk_size: int,
    done: set[int],
    identity: str | None = None,
) -> None:
    payload = {
        "total": total,
        "chunk_size": chunk_size,
        "identity": identity,
        "done": sorted(done),
    }
    temp = sidecar.with_name(sidecar.name + ".tmp")
    try:
        temp.write_text(json.dumps(payload), encoding="utf-8")
        temp.replace(sidecar)  # 原子替换，避免半截 JSON
    except OSError as exc:
        log.warning("无法写入断点元数据 %s：%s", sidecar, exc)


def _primary_error(group: BaseExceptionGroup) -> BaseException:
    """从 TaskGroup 抛出的异常组里挑出最有信息量的那个。"""
    errors: list[BaseException] = []

    def collect(exc: BaseException) -> None:
        if isinstance(exc, BaseExceptionGroup):
            for child in exc.exceptions:
                collect(child)
        else:
            errors.append(exc)

    collect(group)
    for error in errors:
        if isinstance(error, BiliError):
            return error
    return errors[0] if errors else group
