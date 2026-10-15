"""``--check`` 的退出码与文案。

这是用户遇到问题时第一个会跑的命令，它的退出码既给人看也给脚本读，所以每个
分支都要锁住。

这里还锁住一条容易回退的结论：**ffmpeg 的有无不再是失败**。MP4 合并已经内置，
没有 ffmpeg 照样能下能合，所以它只影响 ``--container mkv``，不该占用退出码，
更不该把真正的网络/登录故障盖掉。
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
    merge_failure: str | None = None,
) -> io.StringIO:
    """装好各项检查的外部依赖，返回捕获输出的缓冲区。"""
    out = io.StringIO()
    monkeypatch.setattr(cli, "find_ffmpeg", lambda: ffmpeg)
    monkeypatch.setattr(cli, "check_merge", lambda: merge_failure)
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


async def test_missing_ffmpeg_is_not_a_failure(_patched) -> None:
    """没有 ffmpeg 也能下能合 MP4，所以退出码必须是 0。

    但也不能只字不提——用户将来想用 ``--container mkv`` 时会需要它，所以文案里
    要讲清楚它只影响哪件事。
    """
    out = _patched(ffmpeg=None, nav=NavData(isLogin=False))
    code, text = await _run(out)
    assert code == ExitCode.OK
    assert "自检通过" in text, "--check 要现场验证合并内核，而不是只声明它是内置的"
    assert "未装 ffmpeg" in text
    assert "mkv" in text.lower(), "要说清楚 ffmpeg 只影响 MKV，否则用户会以为工具坏了"


async def test_broken_merge_kernel_exits_6(_patched) -> None:
    """合并内核自检失败是**安装包坏了**，不是用户环境坏了。

    这条必须占住退出码 6（缺依赖），否则用户会去查网络、重装 ffmpeg，唯独不会想到
    重新装一遍这个工具——而那才是唯一的解法。
    """
    out = _patched(merge_failure="合并产物里找不到 mdat 数据", nav=NavData(isLogin=False))
    code, text = await _run(out)
    assert code == ExitCode.MISSING_DEPENDENCY
    assert "自检失败" in text
    assert "重新安装" in text


async def test_merge_self_check_does_not_mask_a_network_failure(_patched) -> None:
    """自检失败也不能把网络故障的结论顶掉——先发生的故障优先。"""
    out = _patched(merge_failure="坏了", nav=None)
    code, _ = await _run(out)
    assert code == ExitCode.MISSING_DEPENDENCY  # 自检排在网络检查之前


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


async def test_network_failure_surfaces_even_without_ffmpeg(_patched) -> None:
    """ffmpeg 缺席不该盖掉真正的故障。

    早先 ffmpeg 是硬依赖，断网时退出码会被它抢成 6，把人引去装一个跟问题无关的
    东西。现在合并是内置的，网络故障就该如实报成 3。
    """
    out = _patched(ffmpeg=None, nav=None)
    code, text = await _run(out)
    assert code == ExitCode.NETWORK
    assert "无法访问" in text and "自检通过" in text


async def test_expired_cookie_surfaces_even_without_ffmpeg(_patched) -> None:
    out = _patched(ffmpeg=None, nav=NavData(isLogin=False), cookie="dead_beef")
    code, text = await _run(out)
    assert code == ExitCode.AUTH
    assert "SESSDATA" in text


async def test_no_cookie_is_a_valid_state_not_a_failure(_patched) -> None:
    """未登录不是错误——匿名照样能下 480P，退出码必须是 0。"""
    out = _patched(nav=NavData(isLogin=False))
    code, _ = await _run(out)
    assert code == ExitCode.OK
