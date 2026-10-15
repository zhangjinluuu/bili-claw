"""``--check`` 的退出码与文案。

这是用户遇到问题时第一个会跑的命令，它的退出码既给人看也给脚本读，所以每个
分支都要锁住。重点是**多重故障**：没有 ffmpeg 又刚好断网时，退出码必须指向
最该先解决的那个问题（装 ffmpeg），而不是最后被评估到的那个。
"""

from __future__ import annotations

import io
from typing import Any, Self

import pytest
from rich.console import Console

from biliclaw import cli
from biliclaw.cli import Options
from biliclaw.errors import ExitCode
from biliclaw.models import NavData


class _FakeClient:
    """替掉 BiliClient。run_check 只用得到 probe_login()。"""

    def __init__(self, nav: NavData | None) -> None:
        self._nav = nav

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def probe_login(self) -> NavData | None:
        return self._nav


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ffmpeg: str | None = "/opt/homebrew/bin/ffmpeg",
    nav: NavData | None = None,
    cookie: str | None = None,
) -> io.StringIO:
    """装好三项检查的外部依赖，返回捕获输出的缓冲区。"""
    out = io.StringIO()
    monkeypatch.setattr(cli, "find_ffmpeg", lambda: ffmpeg)
    cookies = {"SESSDATA": cookie} if cookie else {}
    monkeypatch.setattr(cli, "resolve_cookie", lambda **_kw: (cookies, "命令行" if cookie else None))
    monkeypatch.setattr(cli, "BiliClient", lambda **_kw: _FakeClient(nav))
    return out


async def _run(out: io.StringIO, **kwargs: Any) -> tuple[int, str]:
    console = Console(file=out, width=200, no_color=True)
    code = await cli.run_check(Options(url="BV1xx411c7mD", check=True, **kwargs), console)
    return code, out.getvalue()


@pytest.fixture
def _patched(monkeypatch: pytest.MonkeyPatch):
    def install(**kwargs: Any):
        return _install(monkeypatch, **kwargs)

    return install


async def test_all_good_anonymous_succeeds(_patched) -> None:
    out = _patched(nav=NavData(isLogin=False))
    code, text = await _run(out)
    assert code == ExitCode.OK
    assert "ffmpeg" in text
    # 匿名用户最需要知道的是"天花板在哪"，不是"接口通不通"。
    assert "480P" in text


async def test_missing_ffmpeg_exits_6(_patched) -> None:
    out = _patched(ffmpeg=None, nav=NavData(isLogin=False))
    code, text = await _run(out)
    assert code == ExitCode.MISSING_DEPENDENCY
    assert "brew install ffmpeg" in text, "缺依赖时必须给出可执行的补救命令"


async def test_network_failure_exits_3(_patched) -> None:
    out = _patched(nav=None)
    code, text = await _run(out)
    assert code == ExitCode.NETWORK
    assert "无法访问" in text


async def test_expired_cookie_exits_4(_patched) -> None:
    """本地有 Cookie 但B站不认：这是登录态失效，不是没登录，也不该让人去查网络。"""
    out = _patched(nav=NavData(isLogin=False), cookie="dead_beef")
    code, text = await _run(out)
    assert code == ExitCode.AUTH
    assert "SESSDATA" in text


async def test_logged_in_vip_reports_4k_ceiling(_patched) -> None:
    out = _patched(nav=NavData(isLogin=True, mid=42, uname="测试用户", vipStatus=1))
    code, text = await _run(out)
    assert code == ExitCode.OK
    assert "测试用户" in text and "42" in text
    assert "大会员" in text and "4K" in text


async def test_logged_in_without_vip_still_caps_at_1080p(_patched) -> None:
    out = _patched(nav=NavData(isLogin=True, mid=42, uname="普通用户", vipStatus=0))
    code, text = await _run(out)
    assert code == ExitCode.OK
    assert "非大会员" in text
    assert "1080P" in text and "大会员" in text


async def test_missing_ffmpeg_outranks_network_failure(_patched) -> None:
    """两个问题同时存在时，退出码要指向最该先解决的那个。

    没有 ffmpeg 时什么都下不了，这是本地、确定、一条命令就能修好的问题；
    而网络故障可能是瞬时的。早期版本会被后评估的网络分支覆盖成 3，把人引偏。
    """
    out = _patched(ffmpeg=None, nav=None)
    code, text = await _run(out)
    assert code == ExitCode.MISSING_DEPENDENCY
    # 两个问题都要如实说出来，只是退出码只能有一个。
    assert "ffmpeg" in text and "无法访问" in text


async def test_missing_ffmpeg_outranks_expired_cookie(_patched) -> None:
    out = _patched(ffmpeg=None, nav=NavData(isLogin=False), cookie="dead_beef")
    code, _ = await _run(out)
    assert code == ExitCode.MISSING_DEPENDENCY


async def test_no_cookie_is_a_valid_state_not_a_failure(_patched) -> None:
    """未登录不是错误——匿名照样能下 480P，退出码必须是 0。"""
    out = _patched(nav=NavData(isLogin=False))
    code, _ = await _run(out)
    assert code == ExitCode.OK
