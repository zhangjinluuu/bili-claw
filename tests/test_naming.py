"""输出路径模板与文件名净化测试（需求文档 FR-07）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bilidl.errors import UsageError
from bilidl.naming import (
    TEMPLATE_FIELDS,
    render_path,
    render_template,
    resolve_collision,
    sanitize_component,
    template_fields,
    validate_template,
)

FIELDS = {
    "title": "【测试】某视频",
    "bvid": "BV1xx411c7mD",
    "aid": 2,
    "p": 1,
    "p_name": "",
    "up": "某UP主",
    "mid": 12345,
    "qn": 80,
    "quality": "1080P",
    "date": "20240101",
    "datetime": "20240101_120000",
}


def test_default_template_renders_up_then_title() -> None:
    assert render_path("{up}/{title}", FIELDS) == Path("某UP主/【测试】某视频")


def test_template_fields_extraction() -> None:
    assert template_fields("{up}/{title}") == {"up", "title"}


def test_template_fields_ignores_format_spec() -> None:
    assert template_fields("{p:02d}/{title}") == {"p", "title"}


def test_format_spec_is_honoured() -> None:
    assert render_template("第{p:02d}P", FIELDS) == "第01P"


def test_validate_template_accepts_known_fields() -> None:
    validate_template("{up}/{title}")


def test_validate_template_rejects_typos() -> None:
    """拼错的变量必须报错，否则会静默生成名叫 `{tile}` 的目录。"""
    with pytest.raises(UsageError) as excinfo:
        validate_template("{up}/{tile}")
    assert "tile" in str(excinfo.value)
    assert "title" in (excinfo.value.hint or "")


def test_validate_template_rejects_unbalanced_braces() -> None:
    with pytest.raises(UsageError):
        validate_template("{up}/{title")


def test_every_documented_field_is_available() -> None:
    for name in TEMPLATE_FIELDS:
        validate_template("{" + name + "}")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a/b", "a_b"),  # 斜杠会改变目录层级
        ("a:b", "a_b"),
        ("a\\b", "a_b"),
        ("a*b?c", "a_b_c"),
        ('a"b<c>d|e', "a_b_c_d_e"),
        ("a\x00b", "a b"),  # 控制字符换成空格，避免把两侧的词粘在一起
        ("  spaced  ", "spaced"),
        ("line\nbreak", "line break"),
    ],
)
def test_sanitize_replaces_path_metacharacters(raw: str, expected: str) -> None:
    assert sanitize_component(raw) == expected


def test_sanitize_strips_trailing_dot_and_space() -> None:
    """Windows 不允许文件名以点或空格结尾，统一按最严的处理。"""
    assert sanitize_component("name. ") == "name"
    assert not sanitize_component("name...").endswith(".")


def test_sanitize_prefixes_reserved_device_names() -> None:
    for reserved in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT1"):
        assert sanitize_component(reserved) == f"_{reserved}"


def test_sanitize_is_case_insensitive_for_reserved_names() -> None:
    assert sanitize_component("con") == "_con"


def test_sanitize_falls_back_for_empty_input() -> None:
    assert sanitize_component("") == "untitled"
    assert sanitize_component("///") == "___"


def test_sanitize_truncates_long_names() -> None:
    result = sanitize_component("あ" * 500, max_length=50)
    assert len(result) == 50


def test_render_path_sanitizes_every_component() -> None:
    fields = dict(FIELDS, title="a/b:c", up="u/p")
    assert render_path("{up}/{title}", fields) == Path("u_p/a_b_c")


def test_render_path_drops_empty_and_dot_components() -> None:
    """`{p_name}/` 这类模板在单P视频上会渲染出空片段，不能变成一层空目录。"""
    assert render_path("{up}/{p_name}/{title}", FIELDS) == Path("某UP主/【测试】某视频")


def test_render_path_rejects_parent_traversal() -> None:
    """模板不能借 `..` 把文件写到目标目录之外。"""
    assert render_path("{up}/../../etc", FIELDS) == Path("某UP主/etc")


def test_render_path_rejects_empty_result() -> None:
    with pytest.raises(UsageError):
        render_path("   ", FIELDS)


def test_render_missing_field_is_a_usage_error() -> None:
    with pytest.raises(UsageError):
        render_template("{up}/{nonexistent}", FIELDS)


def test_resolve_collision_passes_through_when_free() -> None:
    assert resolve_collision(Path("/tmp/a.mp4"), exists=lambda _: False) == Path("/tmp/a.mp4")


def test_resolve_collision_appends_counter() -> None:
    taken = {Path("/tmp/a.mp4"), Path("/tmp/a (1).mp4")}
    assert resolve_collision(Path("/tmp/a.mp4"), exists=lambda p: p in taken) == Path("/tmp/a (2).mp4")


def test_resolve_collision_keeps_suffix() -> None:
    assert resolve_collision(Path("/tmp/a.m4a"), exists=lambda p: p == Path("/tmp/a.m4a")) == Path("/tmp/a (1).m4a")
