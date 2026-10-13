"""登录态（Cookie）加载与脱敏（FR-09）。

优先级：``--cookie`` > ``--cookie-file`` > 环境变量 > 默认路径 ``~/.bilidl/cookies.txt``。
文件格式同时容忍三种写法——标准 Netscape cookies.txt（域名在前）、浏览器插件常见的
「名字在前」变体、以及直接粘贴的一行 Cookie 头。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .errors import UsageError

log = logging.getLogger("bilidl.cookies")

#: 判断「是否已登录」的关键字段。
SESSDATA_KEY = "SESSDATA"

_ENV_NAMES = ("BILIDL_COOKIE", "BILI_COOKIE", "BILI_SESSDATA")
DEFAULT_COOKIE_PATH = Path.home() / ".bilidl" / "cookies.txt"

#: 形如 ``.bilibili.com``。用来判断这一行到底是「域名在前」还是「名字在前」。
_DOMAIN_RE = re.compile(r"\.?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")

#: 脱敏用：出现在日志/异常里的敏感字段都会被替换成 ***。
_REDACT_PATTERN = re.compile(
    r"\b(SESSDATA|bili_jct|DedeUserID|DedeUserID__ckMd5|buvid3|buvid4|buvid_fp|"
    r"bili_ticket|bili_ticket_expires|access_key|SESSDATA__ckMd5|b_nut|_uuid|sid)"
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


def _looks_like_domain(value: str) -> bool:
    return bool(_DOMAIN_RE.match(value.strip()))


def _clean_value(name: str, value: str) -> str:
    """剔除 HTTP 头承载不了的字符。

    浏览器插件导出的 Cookie 文件里偶尔会混进 ``✓`` 之类的标记字符（导出时把布尔列
    渲染成了勾）。这类字符会让 httpx 在发请求那一刻抛 ``UnicodeEncodeError``，
    而用户完全无法把那个报错和「文件里有个勾」联系起来。
    """
    value = value.strip().strip('"')
    cleaned = "".join(ch for ch in value if 0x20 < ord(ch) < 0x7F)
    if cleaned != value:
        log.warning("Cookie %s 的值里有 HTTP 头装不下的字符，已剔除", name)
    return cleaned


def _name_and_value(fields: list[str]) -> tuple[str, str]:
    """从一行制表符分隔的记录里取出 ``(名字, 值)``，两种列序都认。

    标准 Netscape 是「域名 标志 路径 安全 过期 名字 值」，但浏览器插件导出的
    往往是「名字 值 域名 路径 过期 …」（列数还各不相同）。只认前者的后果不是
    报错，而是**安静地解析出一堆垃圾字段**——第 6、7 列在那种文件里是导出工具
    自己的标记列。所以这里靠"哪一列长得像域名、哪一列长得像路径"来定列序。
    """
    if len(fields) < 5:
        return "", ""

    # 名字在前：第 3 列是域名、第 4 列是路径
    if _looks_like_domain(fields[2]) and fields[3].strip().startswith("/"):
        return fields[0].strip(), fields[1].strip()

    # 标准 Netscape，域名在前：第 1 列是域名、第 3 列是路径
    if len(fields) >= 7 and _looks_like_domain(fields[0]) and fields[2].strip().startswith("/"):
        return fields[5].strip(), fields[6].strip()

    return "", ""


def load_from_file(path: Path) -> dict[str, str]:
    """读取 Cookie 文件，自动识别 Netscape 格式（两种列序）与单行 Cookie 头格式。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise UsageError(f"Cookie 文件不存在：{path}") from exc
    except OSError as exc:
        raise UsageError(f"无法读取 Cookie 文件：{path}（{exc.strerror}）") from exc

    # 空文件要先单独说清楚。否则会落到下面"没解析出任何字段"的分支，把人引去查格式——
    # 而真正的原因是内容根本没写进去（编辑器新建了文件、但粘贴没成功）。
    if not text.strip():
        raise UsageError(
            f"Cookie 文件是空的：{path}",
            hint=(
                "把浏览器里的 Cookie 粘贴进去（浏览器开发者工具 → Network → "
                "任意 bilibili.com 请求 → Request Headers → Cookie），然后保存"
            ),
        )

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
        if "\t" not in line:
            continue
        name, value = _name_and_value(line.split("\t"))
        if name:
            cookies[name] = _clean_value(name, value)

    if cookies:
        return cookies

    # 不是 Netscape 格式，按「粘贴的一行 Cookie」处理
    pasted = parse_cookie_string(text.replace("\n", ";"))
    if not pasted:
        raise UsageError(
            f"Cookie 文件里没有解析出任何字段：{path}",
            hint=(
                "支持 Netscape cookies.txt，或直接粘贴浏览器里复制的 Cookie 字符串"
                "（形如 SESSDATA=xxx; bili_jct=yyy）"
            ),
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
