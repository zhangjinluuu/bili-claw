"""B站接口客户端——本项目**唯一**与 B站接口耦合的模块（NFR-5 / R1）。

接口一旦变更，需要改动的地方应该只在这个文件里。上层（quality / downloader / cli）
只消费 ``models`` 里的结构化对象，不接触原始 JSON。

所有请求统一附带 ``Referer: https://www.bilibili.com`` 与浏览器 UA——
CDN 与接口都有防盗链校验，缺了会直接 403。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Container
from typing import Any, Self

import httpx
from pydantic import ValidationError

from . import wbi
from .cookies import redact
from .errors import (
    ApiSchemaError,
    BiliError,
    NetworkError,
    RiskControlError,
    error_from_api_code,
)
from .models import ApiEnvelope, NavData, PlayUrlData, VideoInfo

BASE_URL = "https://api.bilibili.com"
REFERER = "https://www.bilibili.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: playurl 的 ``fnval``：4048 = DASH + HDR + 4K + 杜比音频 + 杜比视界 + 8K + AV1 全开。
#: 全开之后由返回结果告诉我们"实际给到了什么"，而不是由我们假设。
DEFAULT_FNVAL = 4048

#: 设备指纹接口：发一组 ``buvid3`` / ``buvid4``，匿名就能取。
#:
#: 这不是"锦上添花"。playurl 少了 ``buvid3`` 时回的既不是错误码也不是 403，而是
#: **HTTP 200 + code 0 + 只有 ``{"v_voucher": ...}``**——一档清晰度都没有，看着却像
#: 成功。从浏览器取 Cookie 的那条路是碰巧带了 buvid3 才一直正常；扫码登录、以及
#: "只粘一段 SESSDATA"都会撞上它，而错误现场指向的是"这支视频没有可用流"。
FINGER_SPI_PATH = "/x/frontend/finger/spi"

#: ``nav`` 对未登录用户返回 -101「账号未登录」。这不是错误：响应里的
#: ``data.wbi_img`` 依然有效，未登录本身就是我们想知道的信息。
NOT_LOGGED_IN = -101

log = logging.getLogger("biliclaw.api")


async def fetch_buvid(http: httpx.AsyncClient) -> dict[str, str]:
    """取一组设备指纹（``buvid3`` / ``buvid4``）。取不到就返回空字典。

    用调用方的 ``http`` 是为了复用连接池与 UA——这个接口对 UA 也挑。
    """
    response = await http.get(
        BASE_URL + FINGER_SPI_PATH,
        headers={"Referer": REFERER, "User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    data = response.json().get("data") or {}
    b_3, b_4 = data.get("b_3"), data.get("b_4")
    if not b_3:
        return {}
    found = {"buvid3": str(b_3)}
    if b_4:
        found["buvid4"] = str(b_4)
    return found


class BiliClient:
    """异步 B站接口客户端。用法：``async with BiliClient(cookies=...) as client:``"""

    def __init__(
        self,
        *,
        cookies: dict[str, str] | None = None,
        timeout: float = 20.0,
        retries: int = 3,
        min_interval: float = 0.15,
    ) -> None:
        self._cookies = dict(cookies or {})
        self._retries = max(0, retries)
        self._min_interval = max(0.0, min_interval)
        self._last_request_at = 0.0
        self._throttle_lock = asyncio.Lock()
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_day: str | None = None
        self._buvid_tried = False
        self._buvid_lock = asyncio.Lock()

        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Referer": REFERER,
                "Origin": REFERER,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            cookies=self._cookies,
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
        )

    # -- 生命周期 ---------------------------------------------------------- #

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        """供下载器复用同一个连接池（同一套 UA/Referer/Cookie）。"""
        return self._client

    # -- 内部工具 ---------------------------------------------------------- #

    async def _throttle(self) -> None:
        """请求节流，降低触发风控的概率（FR-04.5）。"""
        if self._min_interval <= 0:
            return
        async with self._throttle_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    @staticmethod
    def _unwrap(payload: Any, *, path: str, allow_codes: Container[int] = ()) -> Any:
        try:
            envelope = ApiEnvelope.model_validate(payload)
        except ValidationError as exc:
            raise ApiSchemaError(
                f"接口 {path} 的返回结构不符合预期",
                hint="B站接口可能已变更，请提交 issue 并附上 --debug 输出",
            ) from exc

        if envelope.code != 0 and envelope.code not in allow_codes:
            raise error_from_api_code(envelope.code, envelope.message)
        return envelope.data

    async def ensure_fingerprint(self) -> None:
        """确保后续请求带上设备指纹（``buvid3`` / ``buvid4``）。

        只在真正需要它的地方调用——目前是 playurl 与扫码登录的落盘。别的接口
        不检查这个，为它们多发一次请求只是白花时间。只尝试一次，失败也不影响
        主流程：这是补充，不该变成每条命令的额外故障点。
        """
        if self._buvid_tried or "buvid3" in self._cookies:
            return
        async with self._buvid_lock:
            if self._buvid_tried or "buvid3" in self._cookies:
                return
            # 先置标记再发请求：这个接口要是也挂了，我们不希望每条命令都多等它一次。
            self._buvid_tried = True
            try:
                found = await fetch_buvid(self._client)
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("取设备指纹失败（%s），本次不带 buvid3", exc.__class__.__name__)
                return
            if not found:
                log.debug("设备指纹接口没给出 b_3，本次不带 buvid3")
                return
            self._cookies.update(found)
            for name, value in found.items():
                self._client.cookies.set(name, value)
            log.debug("已补上设备指纹：%s", ", ".join(sorted(found)))

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        allow_codes: Container[int] = (),
    ) -> Any:
        """发起一次 GET，带重试、节流与错误码翻译。"""
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        last_error: BiliError | None = None

        for attempt in range(self._retries + 1):
            if attempt:
                # 指数退避；风控（412）退避得更久一些
                delay = min(0.8 * (2 ** (attempt - 1)), 8.0)
                if isinstance(last_error, RiskControlError):
                    delay *= 2
                log.debug("第 %d 次重试 %s，等待 %.1fs", attempt, path, delay)
                await asyncio.sleep(delay)

            await self._throttle()

            try:
                request_params = clean
                if signed:
                    img_key, sub_key = await self._ensure_wbi()
                    request_params = wbi.sign(clean, img_key, sub_key)
                response = await self._client.get(BASE_URL + path, params=request_params)
            except httpx.TransportError as exc:
                last_error = NetworkError(
                    f"网络请求失败：{exc.__class__.__name__}",
                    hint="检查网络连接或代理设置；B站接口在部分网络环境下不可直连",
                )
                continue

            if response.status_code == 412:
                last_error = RiskControlError(
                    "触发风控校验（HTTP 412）",
                    hint="请降低并发（--concurrency 2）或加限速（--limit-rate 1M），稍后再试",
                )
                continue
            if response.status_code >= 500:
                last_error = NetworkError(f"B站服务端错误（HTTP {response.status_code}）")
                continue
            if response.status_code != 200:
                raise NetworkError(
                    f"接口返回 HTTP {response.status_code}：{path}",
                    hint="若持续出现，可能是接口路径已变更",
                )

            try:
                return self._unwrap(response.json(), path=path, allow_codes=allow_codes)
            except json.JSONDecodeError as exc:
                raise ApiSchemaError(f"接口 {path} 返回的不是 JSON", hint="可能被风控页面拦截") from exc

        assert last_error is not None
        raise last_error

    # -- WBI --------------------------------------------------------------- #

    async def _ensure_wbi(self) -> tuple[str, str]:
        """获取并缓存 WBI 密钥；密钥按天轮换，跨天自动刷新。"""
        today = time.strftime("%Y-%m-%d")
        if self._wbi_keys and self._wbi_day == today:
            return self._wbi_keys

        data = await self._request("/x/web-interface/nav", allow_codes={NOT_LOGGED_IN})
        nav = self._validate(NavData, data, path="/x/web-interface/nav")
        if not nav.wbi_img or not nav.wbi_img.img_url or not nav.wbi_img.sub_url:
            raise ApiSchemaError(
                "nav 接口未返回 wbi_img，无法完成 WBI 签名",
                hint="这是B站接口变更的信号，请提交 issue",
            )

        self._wbi_keys = (
            wbi.key_from_url(nav.wbi_img.img_url),
            wbi.key_from_url(nav.wbi_img.sub_url),
        )
        self._wbi_day = today
        log.debug("已刷新 WBI 密钥（img_key=%s…）", self._wbi_keys[0][:8])
        return self._wbi_keys

    @property
    def cookies(self) -> dict[str, str]:
        """当前请求实际会带上的 Cookie（可能已自动补上设备指纹）。

        扫码登录用它落盘：``buvid3`` 也存进文件，下次启动就不必再向接口要一次。
        """
        return dict(self._cookies)

    def invalidate_wbi(self) -> None:
        """丢弃缓存的 WBI 密钥，下次签名时重新获取。

        风控或密钥轮换后旧的 ``w_rid`` 会持续返回 -403，必须能主动失效重取。
        """
        self._wbi_keys = None
        self._wbi_day = None

    @staticmethod
    def _validate(model, data: Any, *, path: str):
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise ApiSchemaError(
                f"接口 {path} 的字段缺失或类型不符",
                hint=f"{exc.error_count()} 处校验失败；B站接口可能已变更",
            ) from exc

    # -- 业务接口 ---------------------------------------------------------- #

    async def nav(self) -> NavData:
        """导航信息：登录状态 + WBI 密钥来源。

        未登录时接口返回 ``-101``，但 ``data`` 依然可用（``isLogin=false`` 且带
        ``wbi_img``），因此这里把它当作正常结果而不是错误。
        """
        data = await self._request("/x/web-interface/nav", allow_codes={NOT_LOGGED_IN})
        return self._validate(NavData, data, path="/x/web-interface/nav")

    async def view(self, *, bvid: str | None = None, aid: int | None = None) -> VideoInfo:
        """稿件基本信息（含分P列表）。"""
        if not bvid and not aid:
            raise ValueError("view() 需要 bvid 或 aid")
        data = await self._request("/x/web-interface/view", {"bvid": bvid, "aid": aid})
        return self._validate(VideoInfo, data, path="/x/web-interface/view")

    async def playurl(
        self,
        *,
        bvid: str,
        cid: int,
        qn: int = 80,
        fnval: int = DEFAULT_FNVAL,
        fourk: int = 1,
    ) -> PlayUrlData:
        """取播放地址。

        ``qn`` 只是"希望值"——B站会按账号权限静默降级，真实档位以返回的
        ``dash.video[].id`` 为准（见 ``quality`` 模块的说明）。
        """
        await self.ensure_fingerprint()
        data = await self._request(
            "/x/player/wbi/playurl",
            {"bvid": bvid, "cid": cid, "qn": qn, "fnval": fnval, "fourk": fourk},
            signed=True,
        )
        # v_voucher 是风控软挑战：HTTP 200、code 0，但没有任何 dash/durl。不在这里
        # 拦下，上层看到的就是"这支视频没有可用的流"——一句把矛头指向视频和账号的话。
        if "v_voucher" in data and not data.get("dash") and not data.get("durl"):
            raise RiskControlError(
                "B站只回了风控挑战（v_voucher），没有给播放地址",
                hint=(
                    "通常是设备指纹缺失或当前网络被标记。本程序会自动补 buvid3 后重试；"
                    "仍失败就换一次网络，或把浏览器里那份完整 Cookie 用 --cookie 给过来"
                ),
            )
        return self._validate(PlayUrlData, data, path="/x/player/wbi/playurl")

    async def probe_login(self) -> NavData | None:
        """尽力探测登录状态；失败时返回 ``None`` 而不抛错（非关键路径）。"""
        try:
            return await self.nav()
        except BiliError as exc:
            log.debug("登录状态探测失败：%s", redact(str(exc)))
            return None
