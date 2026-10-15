"""Cookie 解析与脱敏测试（需求文档 FR-09，安全硬要求）。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from biliclaw.cookies import (
    SESSDATA_KEY,
    cookie_header,
    is_logged_in,
    load_from_file,
    parse_cookie_string,
    redact,
)
from biliclaw.errors import UsageError

NETSCAPE = """# Netscape HTTP Cookie File
.bilibili.com\tTRUE\t/\tFALSE\t1893456000\tSESSDATA\tabc%2Cdef
.bilibili.com\tTRUE\t/\tFALSE\t1893456000\tbili_jct\tdeadbeef
#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1893456000\tDedeUserID\t12345
"""


def test_parse_cookie_string() -> None:
    cookies = parse_cookie_string("SESSDATA=abc; bili_jct=def")
    assert cookies == {"SESSDATA": "abc", "bili_jct": "def"}


def test_parse_cookie_string_tolerates_whitespace_and_trailing_semicolon() -> None:
    assert parse_cookie_string("  SESSDATA = abc ;  ") == {"SESSDATA": "abc"}


def test_parse_cookie_string_strips_quotes() -> None:
    assert parse_cookie_string('SESSDATA="abc"') == {"SESSDATA": "abc"}


def test_parse_cookie_string_keeps_values_containing_equals() -> None:
    """base64 值里常有 `=`，按第一个等号切分才对。"""
    assert parse_cookie_string("SESSDATA=ab=cd") == {"SESSDATA": "ab=cd"}


def test_parse_cookie_string_skips_junk() -> None:
    assert parse_cookie_string(";;novalue; a=b") == {"a": "b"}


def test_load_netscape_file(tmp_path: Path) -> None:
    path = tmp_path / "cookies.txt"
    path.write_text(NETSCAPE, encoding="utf-8")
    cookies = load_from_file(path)
    assert cookies[SESSDATA_KEY] == "abc%2Cdef"
    assert cookies["bili_jct"] == "deadbeef"
    assert cookies["DedeUserID"] == "12345"


def test_load_netscape_skips_ordinary_comments(tmp_path: Path) -> None:
    path = tmp_path / "cookies.txt"
    path.write_text(NETSCAPE, encoding="utf-8")
    assert "# Netscape" not in load_from_file(path)


def test_load_pasted_single_line_cookie(tmp_path: Path) -> None:
    """从浏览器复制的 Cookie 是一整行，也该能用。"""
    path = tmp_path / "cookies.txt"
    path.write_text("SESSDATA=abc; bili_jct=def", encoding="utf-8")
    assert load_from_file(path) == {"SESSDATA": "abc", "bili_jct": "def"}


def test_load_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as excinfo:
        load_from_file(tmp_path / "nope.txt")
    assert "不存在" in excinfo.value.message


def test_load_unparseable_file_is_a_usage_error(tmp_path: Path) -> None:
    path = tmp_path / "cookies.txt"
    path.write_text("这不是 cookie", encoding="utf-8")
    with pytest.raises(UsageError):
        load_from_file(path)


def test_is_logged_in_requires_sessdata() -> None:
    assert is_logged_in({SESSDATA_KEY: "x"})
    assert not is_logged_in({})
    assert not is_logged_in({"bili_jct": "x"})
    assert not is_logged_in({SESSDATA_KEY: "   "})


def test_cookie_header_is_a_single_line() -> None:
    header = cookie_header({"a": "1", "b": "2"})
    assert header == "a=1; b=2"
    assert "\n" not in header


def test_cookie_header_empty() -> None:
    assert cookie_header({}) == ""


# --------------------------------------------------------------------------- #
# 脱敏：绝不能把凭据写进日志
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "access_key"],
)
def test_redact_hides_every_credential(name: str) -> None:
    redacted = redact(f"Cookie: {name}=supersecret")
    assert "supersecret" not in redacted
    assert "***" in redacted


def test_redact_is_case_insensitive() -> None:
    assert "supersecret" not in redact("sessdata=supersecret")


def test_redact_handles_colon_form() -> None:
    assert "supersecret" not in redact("SESSDATA: supersecret")


def test_redact_leaves_harmless_text_alone() -> None:
    assert redact("正在下载 BV1xx411c7mD") == "正在下载 BV1xx411c7mD"


def test_redact_handles_a_whole_cookie_header() -> None:
    header = "SESSDATA=aaa; bili_jct=bbb; DedeUserID=123"
    redacted = redact(header)
    for secret in ("aaa", "bbb", "123"):
        assert secret not in redacted


def test_redact_does_not_touch_other_cookies_values_by_accident() -> None:
    """脱敏只针对凭据字段，不能把整个字符串都吃掉。"""
    redacted = redact("lang=zh-CN; SESSDATA=secret")
    assert "zh-CN" in redacted
    assert "secret" not in redacted


def test_empty_cookie_file_is_reported_as_empty(tmp_path: Path) -> None:
    """用户建了文件但内容没写进去（编辑器新建、粘贴失败）是很常见的情况。

    笼统地说"没解析出任何字段"会把人引去查格式，而格式根本没问题。
    """
    path = tmp_path / "cookies.txt"
    path.write_text("")
    with pytest.raises(UsageError) as info:
        load_from_file(path)
    assert "空" in info.value.message


def test_whitespace_only_cookie_file_is_also_empty(tmp_path: Path) -> None:
    path = tmp_path / "cookies.txt"
    path.write_text("\n\n   \n\t\n")
    with pytest.raises(UsageError) as info:
        load_from_file(path)
    assert "空" in info.value.message


def test_malformed_cookie_file_keeps_the_format_hint(tmp_path: Path) -> None:
    """有内容但不是任何已知格式时，提示要说清支持哪两种写法。"""
    path = tmp_path / "cookies.txt"
    path.write_text("这不是 cookie，只是一段随便的文字\n")
    with pytest.raises(UsageError) as info:
        load_from_file(path)
    assert "Netscape" in (info.value.hint or "")


def test_world_readable_cookie_file_is_flagged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Cookie 是长期凭据，同机器上别的账号不该读得到（FR-09.4）。"""
    path = tmp_path / "cookies.txt"
    path.write_text("SESSDATA=abc", encoding="utf-8")
    path.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="biliclaw.cookies"):
        assert load_from_file(path) == {"SESSDATA": "abc"}

    assert "权限过宽" in caplog.text
    assert "600" in caplog.text


def test_private_cookie_file_is_quiet(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "cookies.txt"
    path.write_text("SESSDATA=abc", encoding="utf-8")
    path.chmod(0o600)

    with caplog.at_level(logging.WARNING, logger="biliclaw.cookies"):
        load_from_file(path)

    assert caplog.text == ""
