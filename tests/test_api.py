"""``BiliClient`` 的请求层：重试、风控识别与错误码翻译。

这一层是「B站说了什么」与「用户看到什么」之间唯一的翻译官，而它的分支（412、5xx、
传输异常、非 JSON）在真实环境里都很难按需复现——风控尤其不可能为了测试去故意触发，
那既不负责任也会连累正常使用。所以用 ``httpx.MockTransport`` 把这些响应造出来。

E2E-13 要求「识别 HTTP 412 / -352，提示降速重试，退出码 5」，这里就是它的验收。
"""

from __future__ import annotations

import httpx
import pytest

from biliclaw.api import BiliClient
from biliclaw.errors import (
    ApiSchemaError,
    AuthError,
    ExitCode,
    NetworkError,
    RiskControlError,
    VideoUnavailableError,
)


async def _client(handler, *, retries: int = 0, min_interval: float = 0.0) -> BiliClient:
    """构造一个把请求交给 ``handler`` 的客户端，全程不走网络。"""
    client = BiliClient(retries=retries, min_interval=min_interval)
    # 真实连接池在这里用不上，先关掉，免得测试留下未关闭的 AsyncClient。
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _envelope(code: int = 0, data: object = None, message: str = "") -> dict[str, object]:
    return {"code": code, "message": message, "ttl": 1, "data": {} if data is None else data}


# --------------------------------------------------------------------------- #
# 风控（HTTP 412 / 业务码 -352）—— E2E-13


async def test_http_412_is_recognised_as_risk_control() -> None:
    """412 是"你请求太快了"，必须与普通网络错误区分开：对策完全不同。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412)

    client = await _client(handler)
    with pytest.raises(RiskControlError) as info:
        await client._request("/x/web-interface/view")
    await client.aclose()

    assert info.value.exit_code is ExitCode.RISK_CONTROL
    # 提示必须给出可操作的动作，而不是只说"失败了"
    assert "--limit-rate" in (info.value.hint or "") or "--concurrency" in (info.value.hint or "")


async def test_412_is_retried_until_retries_are_exhausted() -> None:
    """风控是可能自愈的（等一会儿就好），所以要重试；但不能无限重试。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(412)

    client = await _client(handler, retries=2)
    with pytest.raises(RiskControlError):
        await client._request("/x/web-interface/view")
    await client.aclose()

    assert calls == 3, "retries=2 表示首次 + 2 次重试"


async def test_412_backs_off_twice_as_long_as_an_ordinary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """风控的退避要加倍：它明说"你太快了"，按普通节奏重试只会火上浇油。"""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("biliclaw.api.asyncio.sleep", fake_sleep)

    async def risk(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412)

    client = await _client(risk, retries=1)
    with pytest.raises(RiskControlError):
        await client._request("/x/web-interface/view")
    await client.aclose()

    async def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = await _client(server_error, retries=1)
    with pytest.raises(NetworkError):
        await client._request("/x/web-interface/view")
    await client.aclose()

    assert len(waits) == 2, f"每次调用应各退避一次，实际 {waits}"
    assert waits[0] == pytest.approx(waits[1] * 2), f"412 的退避应加倍：{waits}"


@pytest.mark.parametrize("code", [-352])
async def test_risk_control_business_code_is_translated(code: int) -> None:
    """除 HTTP 412 外，接口也可能用业务码表达风控。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(code=code, message="风控校验失败"))

    client = await _client(handler)
    with pytest.raises(RiskControlError):
        await client._request("/x/web-interface/view")
    await client.aclose()


async def test_business_code_is_not_retried() -> None:
    """业务码是确定性答复，重试没有意义，只会拖慢报错。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_envelope(code=-352, message="风控"))

    client = await _client(handler, retries=3)
    with pytest.raises(RiskControlError):
        await client._request("/x/web-interface/view")
    await client.aclose()

    assert calls == 1


# --------------------------------------------------------------------------- #
# 其余失败分支


async def test_transport_error_becomes_network_error_and_is_retried() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    client = await _client(handler, retries=2)
    with pytest.raises(NetworkError) as info:
        await client._request("/x/web-interface/view")
    await client.aclose()

    assert calls == 3
    assert info.value.exit_code is ExitCode.NETWORK


@pytest.mark.parametrize("status", [403, 404, 418])
async def test_other_non_200_status_is_not_retried(status: int) -> None:
    """4xx（除 412）是确定性拒绝，重试不会变好。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    client = await _client(handler, retries=3)
    with pytest.raises(NetworkError):
        await client._request("/x/web-interface/view")
    await client.aclose()

    assert calls == 1


async def test_non_json_body_is_reported_as_schema_change() -> None:
    """被风控页面拦下时返回的是 HTML，不是 JSON——要说清而不是抛 JSONDecodeError。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>拦截</html>")

    client = await _client(handler)
    with pytest.raises(ApiSchemaError):
        await client._request("/x/web-interface/view")
    await client.aclose()


async def test_auth_error_code_maps_to_exit_4() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(code=-403, message="访问权限不足"))

    client = await _client(handler)
    with pytest.raises(AuthError) as info:
        await client._request("/x/web-interface/view")
    await client.aclose()

    assert info.value.exit_code is ExitCode.AUTH


async def test_unavailable_video_code_maps_to_exit_2() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(code=62002, message="稿件不可见"))

    client = await _client(handler)
    with pytest.raises(VideoUnavailableError) as info:
        await client._request("/x/web-interface/view")
    await client.aclose()

    assert info.value.exit_code is ExitCode.USAGE


async def test_allow_codes_lets_a_known_nonzero_code_through() -> None:
    """有的接口用非 0 码表达正常状态（如 nav 匿名返回 -101），必须能放行。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(code=-101, data={"isLogin": False}))

    client = await _client(handler)
    data = await client._request("/x/web-interface/nav", allow_codes={-101})
    await client.aclose()

    assert data == {"isLogin": False}


async def test_none_params_are_dropped() -> None:
    """值为 None 的参数不能发出去：B站会把 ``qn=None`` 当成非法值。"""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_envelope(data={}))

    client = await _client(handler)
    await client._request("/x/web-interface/view", {"bvid": "BV1xx411c7mD", "aid": None})
    await client.aclose()

    assert "aid" not in seen[0]
    assert "bvid=BV1xx411c7mD" in seen[0]


# --------------------------------------------------------------------------- #
# 设备指纹（buvid3）—— 没有它 playurl 只回风控挑战
# --------------------------------------------------------------------------- #


async def test_ensure_fingerprint_fills_in_buvid3_and_only_asks_once() -> None:
    """设备指纹只取一次：它是补充信息，不该变成每条命令的固定开销。"""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_envelope(data={"b_3": "DEVICE", "b_4": "DEVICE4"}))

    client = await _client(handler)
    await client.ensure_fingerprint()
    await client.ensure_fingerprint()
    await client.aclose()

    assert seen == ["/x/frontend/finger/spi"]
    assert client.cookies["buvid3"] == "DEVICE"
    assert client.cookies["buvid4"] == "DEVICE4"


async def test_ensure_fingerprint_respects_a_buvid3_the_caller_already_has() -> None:
    """从浏览器取来的 Cookie 自带 buvid3，那就一个请求都不要多发。"""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_envelope(data={"b_3": "NEW"}))

    client = BiliClient(cookies={"buvid3": "MINE"}, retries=0, min_interval=0.0)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.ensure_fingerprint()
    await client.aclose()

    assert seen == []
    assert client.cookies["buvid3"] == "MINE"


async def test_playurl_turns_a_v_voucher_reply_into_risk_control() -> None:
    """只有 v_voucher、没有 dash：这是风控软挑战，不是「这支视频没有可用的流」。

    现场最误导的地方在于它是 HTTP 200 + code 0——一层层看下来都像成功。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(data={"v_voucher": "voucher_x"}))

    client = await _client(handler)
    # 直接给 WBI 密钥，跳过 nav 那一步：这里要验的是对 playurl 返回值的判断
    client._wbi_keys = ("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210")
    import time

    client._wbi_day = time.strftime("%Y-%m-%d")

    with pytest.raises(RiskControlError, match="v_voucher"):
        await client.playurl(bvid="BV1xx411c7mD", cid=1)
    await client.aclose()
