"""错误码映射与退出码测试（需求文档 FR-11 / FR-12）。"""

from __future__ import annotations

import pytest

from biliclaw.errors import (
    API_ERROR_HINTS,
    ApiSchemaError,
    AuthError,
    BiliError,
    ExitCode,
    FileSystemError,
    LinkExpiredError,
    MissingDependencyError,
    NetworkError,
    QualityUnavailableError,
    RiskControlError,
    UsageError,
    VideoUnavailableError,
    error_from_api_code,
)


def test_exit_codes_match_the_spec() -> None:
    """退出码是脚本调用方的接口，改动即破坏兼容性。"""
    assert ExitCode.OK == 0
    assert ExitCode.PARTIAL_FAILURE == 1
    assert ExitCode.USAGE == 2
    assert ExitCode.NETWORK == 3
    assert ExitCode.AUTH == 4
    assert ExitCode.RISK_CONTROL == 5
    assert ExitCode.MISSING_DEPENDENCY == 6
    assert ExitCode.FILESYSTEM == 7


def test_exit_codes_are_distinct() -> None:
    assert len(set(ExitCode)) == len(ExitCode)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (UsageError("x"), ExitCode.USAGE),
        (VideoUnavailableError("x"), ExitCode.USAGE),
        (QualityUnavailableError("x"), ExitCode.USAGE),
        (AuthError("x"), ExitCode.AUTH),
        (RiskControlError("x"), ExitCode.RISK_CONTROL),
        (NetworkError("x"), ExitCode.NETWORK),
        (LinkExpiredError("x"), ExitCode.NETWORK),
        (MissingDependencyError("x"), ExitCode.MISSING_DEPENDENCY),
        (FileSystemError("x"), ExitCode.FILESYSTEM),
        (ApiSchemaError("x"), ExitCode.NETWORK),
    ],
)
def test_exception_to_exit_code(exc: BiliError, expected: ExitCode) -> None:
    assert exc.exit_code == expected


def test_render_includes_the_hint() -> None:
    assert UsageError("出错了", hint="这样做").render() == "出错了\n  → 这样做"


def test_render_without_hint_is_just_the_message() -> None:
    assert NetworkError("超时了").render() == "超时了"


def test_message_is_preserved() -> None:
    assert UsageError("具体原因").message == "具体原因"


@pytest.mark.parametrize("code", [-352])
def test_risk_control_codes(code: int) -> None:
    assert isinstance(error_from_api_code(code), RiskControlError)


@pytest.mark.parametrize("code", [-403, -10403])
def test_auth_codes(code: int) -> None:
    assert isinstance(error_from_api_code(code), AuthError)


@pytest.mark.parametrize("code", [-400, -404])
def test_usage_codes(code: int) -> None:
    error = error_from_api_code(code)
    assert isinstance(error, UsageError)
    assert not isinstance(error, VideoUnavailableError)


@pytest.mark.parametrize("code", [62002, 62004])
def test_unavailable_video_codes(code: int) -> None:
    assert isinstance(error_from_api_code(code), VideoUnavailableError)


@pytest.mark.parametrize("code", [87007, 87008])
def test_charging_exclusive_codes_are_unsupported(code: int) -> None:
    error = error_from_api_code(code)
    assert isinstance(error, UsageError)
    assert "充电" in error.message


def test_every_documented_code_has_a_hint() -> None:
    """FR-12 要求每个已知错误码都给用户一句可行动的建议。"""
    for code, (_, hint) in API_ERROR_HINTS.items():
        assert hint, f"错误码 {code} 缺少提示"


def test_unknown_code_falls_back_to_the_server_message() -> None:
    error = error_from_api_code(-99999, "服务器说不行")
    assert isinstance(error, BiliError)
    assert error.message == "服务器说不行"


def test_unknown_code_without_message_still_reads_well() -> None:
    error = error_from_api_code(-99999)
    assert "-99999" in error.message


def test_hint_is_actionable_for_the_membership_case() -> None:
    """-10403 的重点是告诉用户"还有更低的档位可用"，而不是只说不行。"""
    error = error_from_api_code(-10403)
    assert "-q" in (error.hint or "") or "档位" in (error.hint or "")


def test_risk_control_hint_mentions_the_two_real_causes() -> None:
    hint = error_from_api_code(-352).hint or ""
    assert "并发" in hint or "限速" in hint
    assert "Cookie" in hint


def test_content_errors_keep_the_usage_exit_code_but_are_not_usage_errors() -> None:
    """「这支视频没有这一档」退出码沿用 2，但用户一个字都没敲错。

    这个区别在 ``cli.main`` 里是可见的：只有 ``UsageError`` 才补打整页帮助。
    把内容不可用做成 ``UsageError``，用户下载失败时会被糊一屏选项表——矛头指错了人。
    """
    for error in (VideoUnavailableError("x"), QualityUnavailableError("x")):
        assert error.exit_code == ExitCode.USAGE
        assert not isinstance(error, UsageError)
