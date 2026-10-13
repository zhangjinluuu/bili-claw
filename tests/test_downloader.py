"""分片规划与限速解析测试（需求文档 FR-04 / NFR-6）。"""

from __future__ import annotations

import itertools

import pytest

from bilidl.downloader import Chunk, parse_rate, plan_chunks


def test_chunk_length_is_inclusive() -> None:
    """Range 是闭区间：bytes=0-3 是 4 个字节，差一位就会损坏文件。"""
    assert Chunk(index=0, start=0, end=3).length == 4


def test_range_header_format() -> None:
    assert Chunk(index=0, start=0, end=1048575).range_header() == "bytes=0-1048575"


def test_plan_chunks_of_zero_is_empty() -> None:
    assert plan_chunks(0, 1024) == []


def test_plan_chunks_of_negative_is_empty() -> None:
    assert plan_chunks(-5, 1024) == []


def test_plan_chunks_exact_multiple() -> None:
    chunks = plan_chunks(8, 4)
    assert [(c.start, c.end) for c in chunks] == [(0, 3), (4, 7)]


def test_plan_chunks_remainder_goes_to_the_last_chunk() -> None:
    chunks = plan_chunks(10, 4)
    assert [(c.start, c.end) for c in chunks] == [(0, 3), (4, 7), (8, 9)]
    assert chunks[-1].length == 2


def test_plan_chunks_smaller_than_one_chunk() -> None:
    chunks = plan_chunks(3, 1024)
    assert len(chunks) == 1
    assert chunks[0].range_header() == "bytes=0-2"


def test_plan_chunks_covers_every_byte_exactly_once() -> None:
    """分片既不能重叠也不能留空洞——否则文件静默损坏。"""
    total = 10_000_017
    chunks = plan_chunks(total, 4 * 1024 * 1024)
    assert chunks[0].start == 0
    assert chunks[-1].end == total - 1
    assert sum(chunk.length for chunk in chunks) == total
    for previous, current in itertools.pairwise(chunks):
        assert current.start == previous.end + 1


def test_plan_chunks_indexes_are_sequential() -> None:
    chunks = plan_chunks(100, 10)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_plan_chunks_guards_against_a_zero_chunk_size() -> None:
    """chunk_size=0 会让循环永不前进；必须退化成逐字节而不是挂死。"""
    chunks = plan_chunks(3, 0)
    assert sum(chunk.length for chunk in chunks) == 3


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1M", 1024**2),
        ("1m", 1024**2),
        ("500K", 500 * 1024),
        ("2G", 2 * 1024**3),
        ("800000", 800_000),
        ("1.5M", 1.5 * 1024**2),
        ("1M/s", 1024**2),
        ("1mbps", 1024**2),
    ],
)
def test_parse_rate(text: str, expected: float) -> None:
    assert parse_rate(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [None, "", "   ", "unlimited", "fast", "0", "-1M"])
def test_parse_rate_returns_none_for_unlimited_or_invalid(text: str | None) -> None:
    assert parse_rate(text) is None
