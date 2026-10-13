"""登录态（Cookie）加载与脱敏（FR-09）。

优先级：``--cookie`` > ``--cookie-file`` > 环境变量 > 默认路径 ``~/.bilidl/cookies.txt``。
文件格式同时容忍 Netscape cookies.txt 与「直接粘贴的一行 Cookie 头」两种写法——
前者是浏览器插件的导出格式，后者是用户从开发者工具里复制出来的。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import UsageError

#: 判断「是否已登录」的关键字段。
SESSDATA_KEY = "SESSDATA"

_ENV_NAMES = ("BILIDL_COOKIE", "BILI_COOKIE")
DEFAULT_COOKIE_PATH = Path.home() / ".bilidl" / "cookies.txt"

#: 脱敏用：出现在日志/异常里的敏感字段都会被替换成 ***。
_REDACT_PATTERN = re.compile(
    r"\b(SESSDATA|bili_jct|DedeUserID|DedeUserID__ckMd5|buvid3|access_key|SESSDATA__ckMd5)"
    r"\s*[=:]\s*([^;,\s&\"']+)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """把文本里的 Cookie 值替换为 ***，用于任何可能被打印的地方。"""
    return _REDACT_PATTERN.sub(r"\1=***", text)


def parse_cookie_string(raw: str) -> dict[str, str]:
    """解析 ``a=1; b=2`` 形式的 Cookie 头。"""
    cookies: dict[str, str] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if name:
            cookies[name] = value
    return cookies


def load_from_file(path: Path) -> dict[str, str]:
    """读取 Cookie 文件，自动识别 Netscape 格式与单行 Cookie 头格式。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise UsageError(f"Cookie 文件不存在：{path}") from exc
    except OSError as exc:
        raise UsageError(f"无法读取 Cookie 文件：{path}（{exc.strerror}）") from exc

    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Netscape 格式里 HttpOnly 字段以 "#HttpOnly_" 前缀标注，需要剥掉再解析
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7:  # domain, flag, path, secure, expiry, name, value
            cookies[fields[5]] = fields[6]

    if cookies:
        return cookies

    # 不是 Netscape 格式，按「粘贴的一行 Cookie」处理
    pasted = parse_cookie_string(text.replace("\n", ";"))
    if not pasted:
        raise UsageError(
            f"Cookie 文件里没有解析出任何字段：{path}",
            hint="支持 Netscape cookies.txt，或直接粘贴浏览器里复制的 Cookie 字符串",
        )
    return pasted


def resolve_cookie(
    *,
    explicit: str | None = None,
    cookie_file: Path | None = None,
    use_default_file: bool = True,
) -> tuple[dict[str, str], str | None]:
    """按优先级解析 Cookie，返回 ``(cookies, 来源描述)``。

    没有任何来源时返回 ``({}, None)``——未登录是合法状态，由上层决定是否提示。
    """
    if explicit:
        return parse_cookie_string(explicit), "--cookie"

    if cookie_file is not None:
        return load_from_file(Path(cookie_file).expanduser()), str(cookie_file)

    for name in _ENV_NAMES:
        raw = os.environ.get(name)
        if raw:
            return parse_cookie_string(raw), f"环境变量 {name}"

    if use_default_file and DEFAULT_COOKIE_PATH.is_file():
        return load_from_file(DEFAULT_COOKIE_PATH), str(DEFAULT_COOKIE_PATH)

    return {}, None


def is_logged_in(cookies: dict[str, str]) -> bool:
    """只有存在非空的 SESSDATA 才算登录态，其余一律按未登录处理。"""
    return bool(cookies.get(SESSDATA_KEY, "").strip())


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())
