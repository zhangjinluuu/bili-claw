"""B站 App 扫码登录。

网页版扫码登录就是两个**匿名**接口：

- ``generate`` 返回 ``(url, qrcode_key)``。``url`` 是要编进二维码的内容，
  ``qrcode_key`` 是这次登录会话的句柄；
- ``poll`` 拿 ``qrcode_key`` 问状态，**成功那一次**的响应里才带登录态。

为什么二维码必须在本地生成：``qrcode_key`` 就是这次登录的凭据。谁拿到它，谁就可以
用自己的手机扫完，然后**被扫码的这一端存下的是他的账号**——用户以为登进了自己的号，
实际存的是别人的 Cookie。所以这里只依赖一个本地编码库，绝不把 url 发给任何外部
二维码服务。

两个接口都不需要登录态，也不校验 UA，但这里仍然带上和主客户端一致的 UA：裸的
httpx 默认 UA 在 passport 域名上偶发被风控。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse

import httpx
import segno

from .api import REFERER, USER_AGENT
from .errors import AuthError, NetworkError

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# poll 的 data.code。这几个值不是猜的，是接口实际返回的：
WAITING = 86101  # 还没人扫
SCANNED = 86090  # 手机上扫了，但还没点「确认登录」
EXPIRED = 86038  # 二维码过期
SUCCESS = 0  # 扫完确认了，这次响应里带登录态

# 成功时这几个字段既出现在 Set-Cookie 里，也出现在 data.url 的查询串里。
# 两处都收：实测有的网络环境下只给其中一处。
COOKIE_FIELDS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")

# 二维码四周的静默区（模块数）。少于 4 个模块，扫描器可能因为找不到边界而失败。
QUIET_ZONE = 4

# 上下两半的明暗组合 → 一个终端字符。配合 "黑字白底" 的样式使用：
# 字符画的是**前景色（黑）**，字符没覆盖到的半边露出**背景色（白）**，
# 于是明暗关系与真正的二维码一致，不依赖终端是深色还是浅色主题。
_HALF_BLOCKS = {
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
    (False, False): " ",
}


@dataclass(frozen=True, slots=True)
class Ticket:
    """一次扫码会话。"""

    url: str
    key: str


@dataclass(frozen=True, slots=True)
class ScanState:
    """poll 的一次结果。"""

    code: int
    message: str = ""

    @property
    def done(self) -> bool:
        return self.code == SUCCESS

    def as_text(self) -> str:
        known = {
            WAITING: "等待扫码…",
            SCANNED: "已扫码，请在手机上点「确认登录」",
            EXPIRED: "二维码已失效",
        }
        return known.get(self.code, self.message or f"未知状态 {self.code}")


@dataclass(frozen=True, slots=True)
class PollResult:
    state: ScanState
    cookies: dict[str, str]


def _headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Referer": REFERER}


async def fetch_ticket(http: httpx.AsyncClient) -> Ticket:
    """向 B站要一个待扫的二维码。

    ``raise_for_status()`` 之后还要看信封里的 ``code``：passport 系列接口即使出错
    也照样返回 200，只看 HTTP 状态码会把「接口变更」当成成功。
    """
    response = await http.get(GENERATE_URL, headers=_headers())
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, None):
        raise NetworkError(
            f"申请扫码登录失败：{payload.get('message') or payload.get('code')}",
            hint="稍后重试；反复失败就用 bclaw --login 从浏览器取 Cookie",
        )
    data = payload.get("data") or {}
    url, key = data.get("url"), data.get("qrcode_key")
    if not url or not key:
        raise NetworkError(
            "扫码登录接口没返回 url / qrcode_key，接口大概改版了",
            hint="这次发版时的接口形状见 qrlogin.py 顶部注释；临时可用 bclaw --login",
        )
    return Ticket(url=str(url), key=str(key))


def _collect_cookies(response: httpx.Response, jar: httpx.Cookies, data: dict) -> dict[str, str]:
    """从一次成功响应里凑齐登录态。

    两个来源都读，并**以 Set-Cookie 为准**（那是浏览器实际会存的），查询串只补
    Set-Cookie 没给的字段。

    注意两边**不是同一个字符串**：跨域 url 里的 SESSDATA 是 URL 编码的，``parse_qsl``
    会替我们解码；Set-Cookie 里则是浏览器存下的字面量。别在这里"顺手统一"——浏览器
    回传的就是那个字面量，改它等于改凭据。
    """
    found: dict[str, str] = {}
    for name in COOKIE_FIELDS:
        value = response.cookies.get(name) or jar.get(name)
        if value:
            found[name] = value

    cross_domain = str(data.get("url") or "")
    if cross_domain:
        for name, value in parse_qsl(urlparse(cross_domain).query, keep_blank_values=False):
            if name in COOKIE_FIELDS and value:
                found.setdefault(name, value)
    return found


async def poll_once(http: httpx.AsyncClient, ticket: Ticket) -> PollResult:
    """问一次状态。网络抖动由调用方（``wait_for_login``）继续轮询。"""
    response = await http.get(POLL_URL, params={"qrcode_key": ticket.key}, headers=_headers())
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    try:
        code = int(data.get("code", -1))
    except (TypeError, ValueError):
        code = -1
    state = ScanState(code=code, message=str(data.get("message") or ""))
    cookies = _collect_cookies(response, http.cookies, data) if state.done else {}
    return PollResult(state=state, cookies=cookies)


async def wait_for_login(
    http: httpx.AsyncClient,
    ticket: Ticket,
    *,
    interval: float = 2.0,
    max_wait: float = 180.0,
    on_state: object = None,
) -> dict[str, str]:
    """一直问到用户扫完为止。

    ``on_state`` 只在状态**变化**时回调一次：轮询每 2 秒一次，逐次报「等待扫码」
    既刷屏又看不出进展。

    网络错误不终止：用户可能刚好在这一刻断网，下一秒就好了，而二维码还有效。
    真正终止的只有三种——成功、过期、超时。
    """
    notify = on_state if callable(on_state) else None
    deadline = time.monotonic() + max_wait
    last_code: int | None = None

    while True:
        try:
            result = await poll_once(http, ticket)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if time.monotonic() >= deadline:
                raise NetworkError(
                    f"轮询扫码状态时网络一直不通：{exc}",
                    hint="二维码已过期，网络恢复后重新跑 bclaw --login --qrcode",
                ) from exc
            await asyncio.sleep(interval)
            continue

        if result.state.code != last_code:
            last_code = result.state.code
            if notify is not None:
                notify(result.state)

        if result.state.done:
            if not result.cookies:
                raise AuthError(
                    "扫码成功了，但响应里没有任何 Cookie 字段",
                    hint="接口大概改版了；先用 bclaw --login 从浏览器取一份可用的登录态",
                )
            return result.cookies
        if result.state.code == EXPIRED:
            raise AuthError(
                "二维码已失效",
                hint="重新跑一次 bclaw --login --qrcode 换个新码",
            )
        if time.monotonic() >= deadline:
            raise AuthError(
                f"等了 {int(max_wait)} 秒还没人扫码",
                hint="重新跑一次 bclaw --login --qrcode 换个新码",
            )
        await asyncio.sleep(interval)


def render_qr(text: str, *, error: str = "m") -> list[str]:
    """把文本画成终端二维码，返回逐行的字符串。

    为什么是**半块字符**而不是 ``██``：一个终端字符通常约两倍高，用两格宽画一个
    模块（``██``）比例才正；用一格一模块会把码拉成两倍高，手机扫不动。半块字符让
    一个字符格同时承载上下两个模块，横向一格一模块、纵向两模块一格，比例正好。

    也不做颜色反转：调用方用「黑字白底」渲染这几行，于是深色模块就是黑的、浅色
    模块就是白的，与真实二维码一致——不赌扫描器能不能认反色，也不赌终端主题。

    ``error="m"`` 对应约 15% 的容错：终端里画出来的码经常被字距、抗锯齿啃掉一点，
    留出余量比省几个模块重要。
    """
    if not text:
        raise ValueError("二维码内容不能为空")

    matrix = segno.make(text, error=error).matrix
    # segno 的 matrix 不含静默区，自己补一圈：没有它，扫描器可能找不到边界。
    size = len(matrix) + QUIET_ZONE * 2
    grid = [[False] * size for _ in range(size)]
    for y, row in enumerate(matrix):
        target = grid[y + QUIET_ZONE]
        for x, value in enumerate(row):
            target[x + QUIET_ZONE] = bool(value)

    lines: list[str] = []
    for y in range(0, size, 2):
        top = grid[y]
        bottom = grid[y + 1] if y + 1 < size else [False] * size
        # strict=True：上下两行必然等长，不等长说明这张网格构造错了，让它当场炸
        lines.append("".join(_HALF_BLOCKS[(t, b)] for t, b in zip(top, bottom, strict=True)))
    return lines
