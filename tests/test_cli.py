"""输入解析测试：用户粘贴什么都能尽量识别（需求文档 FR-01 / FR-05）。"""

from __future__ import annotations

import pytest

from bilidl.cli import Target, parse_page_spec, parse_target_text
from bilidl.errors import UsageError


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
