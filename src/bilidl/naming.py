"""文件名/目录模板渲染与文件系统净化（FR-07）。

模板里的 ``/`` 是**目录分隔符**：``--template "{up}/{title}"`` 会建出子目录。
渲染后按 ``/`` 切分，对每个片段单独净化，这样标题里自带的 ``/`` 不会意外造出目录，
``..`` 也无法逃出输出根目录。
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from pathlib import Path

from .errors import UsageError

#: 模板可用变量 → 说明（会出现在 --help 与报错提示里）。
TEMPLATE_FIELDS: dict[str, str] = {
    "title": "视频标题",
    "bvid": "BV 号，如 BV1xx411c7mD",
    "aid": "AV 号",
    "p": "分P序号，从 1 开始；可写 {p:02d} 补零",
    "p_name": "分P标题（单P视频为空）",
    "up": "UP 主昵称",
    "mid": "UP 主 UID",
    "qn": "实际清晰度档位号，如 80",
    "quality": "实际清晰度名称，如 1080P",
    "date": "投稿日期 YYYYMMDD",
    "datetime": "投稿日期时间 YYYYMMDD_HHMMSS",
}

#: macOS/Linux 上非法的是 ``/`` 与 NUL，Windows 还禁这些——统一按最严的处理。
_ILLEGAL_CHARS = '<>:"/\\|?*'
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")

#: Windows 保留设备名，作为文件名（含扩展名）时不可用。
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: 单个路径片段的长度上限。macOS 的 APFS 限制是 255 字节，中文按 UTF-8 占 3 字节，
#: 取 120 字符留足余量。
DEFAULT_MAX_COMPONENT = 120


def template_fields(template: str) -> set[str]:
    """取出模板里引用到的变量名（忽略 ``{p:02d}`` 这类格式说明）。"""
    names: set[str] = set()
    try:
        parsed = string.Formatter().parse(template)
        for _, field_name, _, _ in parsed:
            if field_name:
                names.add(field_name.split(".")[0].split("[")[0])
    except ValueError as exc:
        raise UsageError(
            f"模板格式有误：{template}",
            hint=f"（{exc}）花括号需要成对出现，字面量花括号请写成 {{{{ 和 }}}}",
        ) from exc
    return names


def validate_template(template: str) -> None:
    """渲染前校验变量名，避免出现 ``{tile}`` 这种拼错却静默生效的模板。"""
    unknown = template_fields(template) - set(TEMPLATE_FIELDS)
    if unknown:
        available = "、".join(f"{{{name}}}" for name in TEMPLATE_FIELDS)
        raise UsageError(
            f"模板里的变量无法识别：{', '.join(sorted(unknown))}",
            hint=f"可用变量：{available}",
        )


def sanitize_component(text: str, *, replacement: str = "_", max_length: int = DEFAULT_MAX_COMPONENT) -> str:
    """把任意文本净化成安全的单个路径片段。"""
    # 控制字符先换成空格再合并空白：直接删除会把 "line\nbreak" 粘成 "linebreak"。
    cleaned = _CONTROL_CHARS.sub(" ", text)
    for char in _ILLEGAL_CHARS:
        cleaned = cleaned.replace(char, replacement)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = cleaned.strip(". ")  # Windows 不允许片段以点或空格结尾
    if cleaned.upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")
    return cleaned or "untitled"


def render_template(template: str, fields: Mapping[str, object]) -> str:
    """渲染模板；变量缺失会被提前拦下，不会抛 KeyError。"""
    validate_template(template)
    try:
        return template.format_map(dict(fields))
    except (KeyError, IndexError, ValueError) as exc:
        raise UsageError(f"模板渲染失败：{template}", hint=str(exc)) from exc


def render_path(
    template: str,
    fields: Mapping[str, object],
    *,
    max_component: int = DEFAULT_MAX_COMPONENT,
) -> Path:
    """渲染成相对路径，并逐片段净化。

    变量值**先**净化再代入模板：否则标题里的 ``/`` 会把一个文件名撑成一层目录。
    模板自身的 ``/`` 仍然有效，所以 ``{up}/{title}`` 依旧能建两级目录。
    """
    safe_fields = {
        key: sanitize_component(value, max_length=max_component)
        if isinstance(value, str) and value.strip()
        else value
        for key, value in fields.items()
    }
    rendered = render_template(template, safe_fields)
    parts = [
        sanitize_component(part, max_length=max_component)
        for part in rendered.split("/")
        if part.strip() and part.strip() not in {".", ".."}
    ]
    if not parts:
        raise UsageError(
            f"模板渲染后是空路径：{template}",
            hint="检查模板是否只由变量之外的空白字符组成",
        )
    return Path(*parts)


def resolve_collision(path: Path, *, exists=Path.exists) -> Path:
    """目标已存在时追加 `` (1)``、`` (2)``… 直到不冲突。

    ``exists`` 可注入以便测试。
    """
    if not exists(path):
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for index in range(1, 1000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not exists(candidate):
            return candidate
    raise UsageError(f"同名文件过多，无法为 {path.name} 找到可用的文件名")
