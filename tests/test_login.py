"""``--login`` 的行为。

重点是那条**容易被省掉、省掉就失去意义**的一步：拿到 Cookie 之后必须当场向B站验证。
只把文件写下去就报成功，等于把"这份凭据到底有没有用"推迟到下一次下载才暴露——
而那时用户已经以为自己配好了，看到 480P 只会去怪片源。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Self

import pytest
from rich.console import Console

from biliclaw import browser, cli
from biliclaw.cli import Options
from biliclaw.errors import AuthError, ExitCode, NetworkError, UsageError
from biliclaw.models import NavData


class _FakeClient:
    def __init__(self, nav: NavData | None) -> None:
        self._nav = nav

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def probe_login(self) -> NavData | None:
        return self._nav


def _extraction(*, fields: int = 2) -> browser.Extraction:
    cookies = tuple(
        browser.Cookie(name=f"c{index}", value=f"v{index}", domain=".bilibili.com")
        for index in range(fields)
    )
    return browser.Extraction("Google Chrome", "Default", cookies)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """把浏览器与网络两头都换掉，只留下被测的编排逻辑。"""

    def install(*, nav: NavData | None = NavData(isLogin=True, mid=7, uname="测试用户"), fields: int = 2):
        saved: list[Path] = []
        monkeypatch.setattr(cli.browser, "extract", lambda name="auto", **_kw: _extraction(fields=fields))
        monkeypatch.setattr(cli.browser, "save", lambda _extraction, path: saved.append(path) or path)
        monkeypatch.setattr(cli, "BiliClient", lambda **_kw: _FakeClient(nav))
        return saved

    return install


def console_for(out: io.StringIO) -> Console:
    return Console(file=out, width=200, no_color=True)


async def _login(out: io.StringIO, **kwargs: Any) -> Path:
    return await cli.run_login(Options(url="", login=True, **kwargs), console_for(out))


async def test_login_saves_the_file_and_reports_the_ceiling(patched) -> None:
    saved = patched()
    out = io.StringIO()
    path = await _login(out, cookie_file=Path("/tmp/target/cookies.txt"))

    assert path == Path("/tmp/target/cookies.txt")
    assert saved == [Path("/tmp/target/cookies.txt")]
    text = out.getvalue()
    assert "测试用户" in text and "7" in text
    # 用户跑的是一条"让我能下高清"的命令，所以必须回答"现在能下到多高"
    assert "1080P" in text


async def test_login_reports_capabilities_for_a_vip(patched) -> None:
    patched(nav=NavData(isLogin=True, mid=7, uname="大会员用户", vipStatus=1))
    out = io.StringIO()
    await _login(out, cookie_file=Path("/tmp/t/cookies.txt"))
    assert "大会员" in out.getvalue()
    assert "4K" in out.getvalue()


async def test_login_falls_back_to_the_default_cookie_path(patched, monkeypatch, tmp_path: Path) -> None:
    """没给 --cookie-file 时写标准位置，之后所有命令不必再带参数。"""
    patched()
    target = tmp_path / "cookies.txt"
    monkeypatch.setattr(cli, "DEFAULT_COOKIE_PATH", target)
    assert await _login(io.StringIO()) == target


async def test_login_refuses_to_combine_with_an_explicit_cookie(patched) -> None:
    """--login 是去取新凭据，--cookie 是手工给凭据，两个一起写含义不明。"""
    patched()
    with pytest.raises(UsageError) as info:
        await _login(io.StringIO(), cookie="SESSDATA=x", cookie_file=Path("/tmp/t/c.txt"))
    assert "--cookie" in info.value.message


async def test_login_fails_when_bilibili_rejects_the_extracted_cookies(patched) -> None:
    """最要紧的一条：取到了字段、也写进了文件，但B站不认账时绝不能报成功。"""
    patched(nav=NavData(isLogin=False))
    with pytest.raises(AuthError) as info:
        await _login(io.StringIO(), cookie_file=Path("/tmp/t/cookies.txt"))
    assert info.value.exit_code == ExitCode.AUTH
    # 提示要把人引向"去浏览器里登录"，而不是去检查文件格式
    assert "bilibili.com" in (info.value.hint or "")


async def test_login_verification_failure_is_a_network_error(patched) -> None:
    """验证不了 ≠ 凭据无效。文件已经写好了，重取一遍是白费功夫。"""
    patched(nav=None)
    with pytest.raises(NetworkError) as info:
        await _login(io.StringIO(), cookie_file=Path("/tmp/t/cookies.txt"))
    assert info.value.exit_code == ExitCode.NETWORK
    assert "--check" in (info.value.hint or "")


async def test_login_survives_a_browser_with_many_fields(patched) -> None:
    """字段数只是拿来展示的，不该出现在成功判定的任何条件里。"""
    patched(fields=42)
    out = io.StringIO()
    await _login(out, cookie_file=Path("/tmp/t/c.txt"))
    assert "42" in out.getvalue()


async def test_run_stops_after_login_when_no_url_is_given(patched) -> None:
    """`bclaw --login` 只是准备登录态，不该顺手去下载点什么。"""
    patched()
    out = io.StringIO()
    code = await cli.run(Options(url="", login=True, cookie_file=Path("/tmp/t/c.txt")), console_for(out))
    assert code == ExitCode.OK
    assert "测试用户" in out.getvalue()
