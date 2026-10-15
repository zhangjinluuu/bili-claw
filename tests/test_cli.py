"""输入解析测试：用户粘贴什么都能尽量识别（需求文档 FR-01 / FR-05）。"""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console

from biliclaw import cli, quality
from biliclaw.cli import Target, build_fields, main, parse_page_spec, parse_target_text, reproduce_command
from biliclaw.errors import AuthError, ExitCode, NetworkError, QualityUnavailableError, UsageError
from biliclaw.models import NavData, Owner, Page, VideoInfo

# --------------------------------------------------------------------------- #
# 稿件标识
# --------------------------------------------------------------------------- #


def test_bare_bvid() -> None:
    assert parse_target_text("BV1xx411c7mD") == Target(bvid="BV1xx411c7mD", page=None)


def test_full_url() -> None:
    target = parse_target_text("https://www.bilibili.com/video/BV1xx411c7mD")
    assert target.bvid == "BV1xx411c7mD"


def test_url_with_query_and_fragment() -> None:
    target = parse_target_text("https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=333.999#reply")
    assert target.bvid == "BV1xx411c7mD"


def test_url_with_page_parameter() -> None:
    """`?p=3` 是B站分P的常见分享形式，必须认出来。"""
    target = parse_target_text("https://www.bilibili.com/video/BV1xx411c7mD?p=3")
    assert target == Target(bvid="BV1xx411c7mD", page=3)


def test_page_parameter_in_a_longer_query() -> None:
    target = parse_target_text("https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333&p=7&t=1")
    assert target.page == 7


def test_av_number() -> None:
    assert parse_target_text("av12345") == Target(aid=12345, page=None)


def test_av_uppercase() -> None:
    assert parse_target_text("AV12345") == Target(aid=12345, page=None)


def test_av_url() -> None:
    assert parse_target_text("https://www.bilibili.com/video/av12345") == Target(aid=12345, page=None)


def test_whitespace_is_trimmed() -> None:
    assert parse_target_text("  BV1xx411c7mD  ").bvid == "BV1xx411c7mD"


def test_empty_input_is_a_usage_error() -> None:
    with pytest.raises(UsageError):
        parse_target_text("   ")


def test_garbage_input_is_a_usage_error() -> None:
    with pytest.raises(UsageError) as excinfo:
        parse_target_text("https://example.com/hello")
    assert "BV" in (excinfo.value.hint or "")


def test_bvid_is_preferred_over_a_stray_av_in_the_url() -> None:
    """分享链接里可能同时出现 av 号参数，应以路径里的 BV 号为准。"""
    target = parse_target_text("https://www.bilibili.com/video/BV1xx411c7mD?from=av999")
    assert target.bvid == "BV1xx411c7mD"
    assert target.aid is None


# --------------------------------------------------------------------------- #
# 分P表达式
# --------------------------------------------------------------------------- #


def test_single_page() -> None:
    assert parse_page_spec("3", 10) == [3]


def test_all_pages() -> None:
    assert parse_page_spec("all", 3) == [1, 2, 3]


def test_all_pages_single_page_video() -> None:
    assert parse_page_spec("all", 1) == [1]


def test_explicit_range() -> None:
    assert parse_page_spec("2-4", 10) == [2, 3, 4]


def test_open_ended_range_starts_at_one() -> None:
    assert parse_page_spec("-3", 10) == [1, 2, 3]


def test_open_ended_range_ends_at_total() -> None:
    assert parse_page_spec("8-", 10) == [8, 9, 10]


def test_comma_list() -> None:
    assert parse_page_spec("1,3,5", 10) == [1, 3, 5]


def test_mixed_list() -> None:
    assert parse_page_spec("1,4-6", 10) == [1, 4, 5, 6]


def test_pages_are_deduplicated_and_sorted() -> None:
    """重复分P会被下载两次，必须去重。"""
    assert parse_page_spec("3,1,3", 10) == [1, 3]


def test_out_of_range_page_is_a_usage_error() -> None:
    with pytest.raises(UsageError) as excinfo:
        parse_page_spec("99", 10)
    assert "99" in excinfo.value.message
    assert "10" in (excinfo.value.hint or "")


def test_zero_page_is_a_usage_error() -> None:
    with pytest.raises(UsageError):
        parse_page_spec("0", 10)


def test_garbage_page_spec_is_a_usage_error() -> None:
    with pytest.raises(UsageError) as excinfo:
        parse_page_spec("第一P", 10)
    assert "-p" in (excinfo.value.hint or "")


def test_empty_page_spec_defaults_to_first_page() -> None:
    assert parse_page_spec("", 10) == [1]


# --------------------------------------------------------------------------- #
# -q ask 的交互选择（FR-03 第 1 条 / E2E-3）


def _stream(height: int, codec: str = "avc") -> quality.VideoStream:
    return quality.VideoStream(
        qn=32 if height >= 480 else 16,
        codecid={"avc": 7, "hevc": 12, "av1": 13}[codec],
        codec=codec,
        width=int(height * 16 / 9),
        height=height,
        fps=30.0,
        bandwidth=400_000,
        urls=("https://cdn.example/v.m4s",),
    )


def _console() -> Console:
    return Console(file=io.StringIO(), width=200, no_color=True)


def test_prompt_quality_refuses_to_hang_without_a_terminal() -> None:
    """管道/脚本里必须立刻报错，而不是安静地等输入。

    一条自动化命令最糟的失败方式是挂在那里直到超时——用户看到的只是一个不动的
    进程，完全不知道它在等人按键。
    """
    with pytest.raises(UsageError) as info:
        cli._prompt_quality([_stream(480), _stream(360)], ("avc",), _console())

    assert "交互式终端" in info.value.message
    # 提示必须给出可以直接复制去用的替代写法
    assert "480P" in (info.value.hint or "")


def test_prompt_quality_returns_the_chosen_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_can_prompt", lambda _console: True)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "2")
    chosen = cli._prompt_quality([_stream(480, "hevc"), _stream(360, "avc")], ("avc", "hevc"), _console())
    assert chosen.height == 360, "选了第 2 项就必须拿到第 2 项"


def test_prompt_quality_menu_puts_the_best_option_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """第 1 项是最好的那档，直接回车即等于 -q max。"""
    monkeypatch.setattr(cli, "_can_prompt", lambda _console: True)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "1")
    chosen = cli._prompt_quality([_stream(360), _stream(480)], ("avc",), _console())
    assert chosen.height == 480


def test_prompt_quality_errors_when_no_stream_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_can_prompt", lambda _console: True)
    with pytest.raises(QualityUnavailableError):
        cli._prompt_quality([], ("avc",), _console())


def test_can_prompt_survives_closed_stdin() -> None:
    """stdin 已关闭时 isatty() 会抛异常，不能让它冒到用户面前。"""

    class _Closed:
        def isatty(self) -> bool:
            raise ValueError("I/O operation on closed file")

    original = sys.stdin
    sys.stdin = _Closed()  # type: ignore[assignment]
    try:
        assert cli._can_prompt(_console()) is False
    finally:
        sys.stdin = original


def test_every_documented_template_variable_is_actually_provided() -> None:
    """文档里写出来的变量，必须真的给得到。

    这条测试会随着 TEMPLATE_FIELDS 自动生长：以后往文档里加变量却忘了在
    build_fields 里填，立刻就红——而不是等用户下载完才发现文件名里是个空位。
    模板变量拼错时 validate_template 会报错，但"变量存在却永远为空"不会。
    """
    from biliclaw.naming import TEMPLATE_FIELDS

    info = VideoInfo(bvid="BV1xx411c7mD", aid=2, title="标题", pubdate=1700000000, owner=Owner(mid=7, name="UP"))
    page = Page(cid=62131, page=3, part="第三话", duration=60)
    fields = build_fields(info, page, qn=80, label="1080P", codec="avc")

    assert set(TEMPLATE_FIELDS) == set(fields)
    assert fields["cid"] == 62131
    assert fields["pname"] == "第三话"
    assert fields["p_name"] == fields["pname"]  # 两个名字都得能用
    assert fields["codec"] == "avc"


def test_reproduce_command_quotes_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["bili-claw", "BV1xx411c7mD", "-o", "/tmp/my dir"])
    assert reproduce_command() == "bili-claw BV1xx411c7mD -o '/tmp/my dir'"


def test_reproduce_command_redacts_cookie_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-11.5 请用户把命令粘出来求助，FR-09 又不许凭据出现在输出里——两者会打架。"""
    monkeypatch.setattr(
        sys, "argv", ["bili-claw", "BV1xx411c7mD", "--cookie", "SESSDATA=secret123; bili_jct=abc456"]
    )
    command = reproduce_command()
    assert "secret123" not in command
    assert "abc456" not in command
    assert "SESSDATA=***" in command


def test_failure_prints_a_rerun_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["bili-claw", "BV1xx411c7mD"])

    def _boom() -> None:
        raise NetworkError("下载中断")

    monkeypatch.setattr(cli, "app", _boom)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == int(ExitCode.NETWORK)
    assert "重跑：bili-claw BV1xx411c7mD" in capsys.readouterr().err


def test_usage_error_does_not_suggest_rerunning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """参数错了就再跑一遍同一个命令，只会得到同一个错误。"""
    monkeypatch.setattr(sys, "argv", ["bili-claw", "BV1xx411c7mD", "-q", "99999"])

    def _boom() -> None:
        raise UsageError("无法识别的清晰度：99999")

    monkeypatch.setattr(cli, "app", _boom)
    with pytest.raises(SystemExit):
        main()
    assert "重跑" not in capsys.readouterr().err


class _ProbeOnlyClient:
    """只够 `_reject_expired_cookie` 用的假客户端。"""

    def __init__(self, nav: NavData | None) -> None:
        self._nav = nav

    async def probe_login(self) -> NavData | None:
        return self._nav


async def test_expired_cookie_is_named_as_expired_not_as_a_bad_video() -> None:
    """服务端拒掉凭据后，playurl 会返回 quality=0 且 dash=None。

    不先查登录态的话，用户看到的是「该视频没有可用的 DASH 视频流」——一句把矛头
    指向视频、真正的问题却在凭据上的话（FR-09 明确要求提示登录已过期）。
    """
    with pytest.raises(AuthError) as caught:
        await cli._reject_expired_cookie(_ProbeOnlyClient(NavData(isLogin=False)), "--cookie")

    assert caught.value.exit_code == ExitCode.AUTH
    assert "失效" in caught.value.message
    assert "SESSDATA" in (caught.value.hint or "")


async def test_login_probe_failure_does_not_claim_the_cookie_expired() -> None:
    """探不到登录态时不断言：网络抖动不该被说成凭据过期，那会让人白换一次 Cookie。"""
    await cli._reject_expired_cookie(_ProbeOnlyClient(None), "--cookie")


async def test_valid_cookie_passes_the_gate() -> None:
    await cli._reject_expired_cookie(_ProbeOnlyClient(NavData(isLogin=True, uname="某人")), "--cookie")
