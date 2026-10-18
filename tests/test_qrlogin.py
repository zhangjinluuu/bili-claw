"""扫码登录的轮询与终端渲染。

这些用例守的是「出错时说真话」。扫码登录最容易骗人的地方是**看起来成功了**：轮询回
``0`` 却没有 Cookie、状态码没见过、网络断在中间。每一条都必须在写盘之前炸掉，而不是
留下一个空文件，让用户下一次下载才发现自己还是匿名。

接口本身用 ``httpx.MockTransport`` 造，不去打真的 B站——真实接口的形状已经在实现时
手工验过（见 ``qrlogin.py`` 顶部），这里守的是我们**对那个形状的反应**。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import httpx
import pytest

from biliclaw import qrlogin
from biliclaw.errors import AuthError, NetworkError

Ticket = qrlogin.Ticket

#: 一次成功响应里的 Set-Cookie。字段名取自 ``qrlogin.COOKIE_FIELDS``。
_SUCCESS_COOKIES = [
    (b"set-cookie", b"SESSDATA=sess%2C1; Domain=.bilibili.com; Path=/"),
    (b"set-cookie", b"bili_jct=csrf; Domain=.bilibili.com; Path=/"),
    (b"set-cookie", b"DedeUserID=42; Domain=.bilibili.com; Path=/"),
    (b"set-cookie", b"DedeUserID__ckMd5=ck; Domain=.bilibili.com; Path=/"),
    (b"set-cookie", b"sid=s1; Domain=.bilibili.com; Path=/"),
]

#: 成功响应里的 url：浏览器实际会把它当跨域登录跳板。
_CROSS_DOMAIN = (
    "https://passport.biligame.com/crossDomain"
    "?DedeUserID=42&DedeUserID__ckMd5=ck&SESSDATA=sess%2C1&bili_jct=csrf&Expires=1&gourl=x"
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ticket_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "data": {"url": "https://scan.example/qr", "qrcode_key": "KEY"}},
    )


def _poll_response(code: int, message: str = "") -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "data": {"code": code, "message": message}})


def _success_response(*, with_cookies: bool = True, url: str = _CROSS_DOMAIN) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "data": {"code": 0, "message": "", "url": url}},
        headers=_SUCCESS_COOKIES if with_cookies else [],
    )


def _sequence(items: Sequence[httpx.Response | Exception]) -> tuple[list[httpx.Response | Exception], Callable]:
    """按顺序发响应；发完了再被调到就说明我们多轮询了一次。"""
    queue = list(items)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue:
            raise AssertionError(f"轮询次数超出预期，多出来的请求打到了 {request.url}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return queue, handler


# --- 渲染 ------------------------------------------------------------------- #


def test_render_qr_keeps_modules_square() -> None:
    """一个字符格要装下上下两个模块，否则码会被拉成两倍高、手机扫不动。"""
    lines = qrlogin.render_qr("https://scan.example/qr?qrcode_key=0123456789abcdef")

    width = len(lines[0])
    assert all(len(line) == width for line in lines), "每行必须等宽"
    assert len(lines) == (width + 1) // 2, "行数应当是宽度的一半（半块字符）"
    assert set("".join(lines)) <= set(qrlogin._HALF_BLOCKS.values())


def test_render_qr_leaves_a_quiet_zone() -> None:
    """没有静默区，扫描器可能找不到码的边界。"""
    lines = qrlogin.render_qr("https://scan.example/qr")

    margin = qrlogin.QUIET_ZONE
    for line in lines:
        assert line[:margin].strip() == ""
        assert line[-margin:].strip() == ""


def test_render_qr_refuses_an_empty_payload() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        qrlogin.render_qr("")


# --- 要二维码 ----------------------------------------------------------------- #


async def test_fetch_ticket_returns_url_and_key() -> None:
    async with _client(lambda request: _ticket_response()) as http:
        ticket = await qrlogin.fetch_ticket(http)

    assert ticket.url == "https://scan.example/qr"
    assert ticket.key == "KEY"


async def test_fetch_ticket_rejects_an_error_envelope() -> None:
    """passport 接口出错照样回 200，只看 HTTP 状态码会把「接口变了」当成功。"""
    response = httpx.Response(200, json={"code": -101, "message": "账号未登录"})
    async with _client(lambda request: response) as http:
        with pytest.raises(NetworkError, match="账号未登录"):
            await qrlogin.fetch_ticket(http)


async def test_fetch_ticket_notices_a_changed_payload() -> None:
    response = httpx.Response(200, json={"code": 0, "data": {}})
    async with _client(lambda request: response) as http:
        with pytest.raises(NetworkError, match="qrcode_key"):
            await qrlogin.fetch_ticket(http)


# --- 轮询 ------------------------------------------------------------------- #


async def test_wait_for_login_returns_cookies_once_the_phone_confirms() -> None:
    seen: list[qrlogin.ScanState] = []
    _, handler = _sequence(
        [
            _poll_response(qrlogin.WAITING),
            _poll_response(qrlogin.SCANNED),
            _success_response(),
        ]
    )

    async with _client(handler) as http:
        cookies = await qrlogin.wait_for_login(
            http, Ticket(url="u", key="k"), interval=0, max_wait=5, on_state=seen.append
        )

    assert cookies == {
        # 逐字保留 Set-Cookie 里的写法：浏览器存的就是这个字面量，回传时也原样送。
        # 跨域 url 里的同名参数是**解码后**的（parse_qsl 会解），两条路于是不同——
        # 别在这里"顺手统一"，那会把浏览器真实的凭据改掉。
        "SESSDATA": "sess%2C1",
        "bili_jct": "csrf",
        "DedeUserID": "42",
        "DedeUserID__ckMd5": "ck",
        "sid": "s1",
    }
    # 状态只在**变化**时通知一次：每 2 秒报一次「等待扫码」既刷屏又看不出进展
    assert [state.code for state in seen] == [qrlogin.WAITING, qrlogin.SCANNED, qrlogin.SUCCESS]


async def test_wait_for_login_falls_back_to_the_cross_domain_url() -> None:
    """有的网络环境下成功响应只把字段放在 data.url 里，不重复发 Set-Cookie。"""
    _, handler = _sequence([_success_response(with_cookies=False)])

    async with _client(handler) as http:
        cookies = await qrlogin.wait_for_login(http, Ticket(url="u", key="k"), interval=0)

    assert cookies["SESSDATA"] == "sess,1"
    assert cookies["DedeUserID"] == "42"


async def test_success_without_any_cookie_is_not_reported_as_success() -> None:
    """码扫了、服务端说成功，但一个字段都没给——这不是成功，是接口改版。"""
    _, handler = _sequence([_success_response(with_cookies=False, url="")])

    async with _client(handler) as http:
        with pytest.raises(AuthError, match="没有任何 Cookie"):
            await qrlogin.wait_for_login(http, Ticket(url="u", key="k"), interval=0)


async def test_an_expired_qrcode_fails_on_the_first_answer() -> None:
    """过期就立刻报，不必等满 180 秒——用户已经不可能扫上了。"""
    queue, handler = _sequence([_poll_response(qrlogin.EXPIRED)])

    async with _client(handler) as http:
        with pytest.raises(AuthError, match="已失效"):
            await qrlogin.wait_for_login(http, Ticket(url="u", key="k"), interval=0)

    assert queue == [], "过期之后不该再轮询"


async def test_wait_for_login_gives_up_after_max_wait() -> None:
    _, handler = _sequence([_poll_response(qrlogin.WAITING) for _ in range(5)])

    async with _client(handler) as http:
        with pytest.raises(AuthError, match="还没人扫码"):
            await qrlogin.wait_for_login(http, Ticket(url="u", key="k"), interval=0, max_wait=0)


async def test_a_flaky_poll_is_retried_rather_than_aborting() -> None:
    """用户可能刚好在这一刻断网，而二维码还有效——不该因此把整个登录判死。"""
    _, handler = _sequence(
        [
            httpx.ConnectError("网络断了"),
            _poll_response(qrlogin.WAITING),
            _success_response(),
        ]
    )

    async with _client(handler) as http:
        cookies = await qrlogin.wait_for_login(
            http, Ticket(url="u", key="k"), interval=0, max_wait=5
        )

    assert cookies["DedeUserID"] == "42"


async def test_unknown_status_surfaces_the_server_message() -> None:
    """没见过的新状态码要原样透出来，别被我们自己编的文案盖掉。"""
    state = qrlogin.ScanState(code=12345, message="风控校验")
    assert state.as_text() == "风控校验"

    known = qrlogin.ScanState(code=qrlogin.SCANNED)
    assert known.as_text() == "已扫码，请在手机上点「确认登录」"
