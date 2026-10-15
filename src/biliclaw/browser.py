"""一键从本机浏览器取出 B站登录态。

这是 FR-09 的便捷入口：与其让用户去开发者工具里翻 Request Headers，不如直接读
浏览器自己的 Cookie 库。三条硬规矩：

1. **Cookie 的值永不打印**。本模块对外只回报「取到了几个字段」，值只在内存里过一手，
   最终由调用方写进 0600 的文件。
2. **绝不改动浏览器自己的库**。浏览器运行时 SQLite 是锁着的，所以一律先复制到临时
   文件再读，读完即删。
3. **取不到要说清是哪种取不到**。没装受支持的浏览器、装了但没登录过 B站、钥匙串授权
   被拒——是三种完全不同的处境，提示也必须不同，否则用户会朝错误的方向排查。

支持范围：macOS 上的 Chromium 系（Chrome / Edge / Brave / Chromium / Vivaldi）与
Firefox。Safari 的 Cookie 是私有二进制格式且需要「完全磁盘访问权限」，不做支持，
但会给出明确提示而不是静默失败。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import BiliError, MissingDependencyError, UsageError

log = logging.getLogger("biliclaw.browser")

#: 只取这些域下的 Cookie；www./space./live. 等子域靠后缀匹配一并命中。
DOMAIN_SUFFIXES = ("bilibili.com", "bilibili.cn")

#: Chromium 的 AES 参数。三个值都硬编码在 Chromium 源码里，不是可配置项。
_SALT = b"saltysalt"
_ITERATIONS = 1003
_KEY_LENGTH = 16  # AES-128
_IV = b" " * 16  # 16 个空格

#: Chrome 130 起在明文前面拼了一段 SHA256(host_key)，把 Cookie 绑到域名上。
_DOMAIN_HASH_LENGTH = 32

#: 1601-01-01 到 1970-01-01 的秒数。Chromium 的时间戳以 1601 为起点、单位为微秒。
_CHROME_EPOCH_OFFSET = 11_644_473_600


@dataclass(frozen=True, slots=True)
class Cookie:
    """一条待写入文件的 Cookie。"""

    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = True
    expires: int = 0  # Unix 秒；0 表示会话 Cookie

    def netscape_line(self) -> str:
        """Netscape cookies.txt 的一行——`load_from_file` 认这个格式。"""
        return "\t".join(
            [
                self.domain,
                "TRUE" if self.domain.startswith(".") else "FALSE",
                self.path,
                "TRUE" if self.secure else "FALSE",
                str(self.expires),
                self.name,
                self.value,
            ]
        )


@dataclass(frozen=True, slots=True)
class Extraction:
    """一次提取的结果。`mapping` 直接可以喂给 `BiliClient`。"""

    browser: str
    profile: str
    cookies: tuple[Cookie, ...]

    @property
    def mapping(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self.cookies}

    def describe(self) -> str:
        return f"{self.browser} · {self.profile} · {len(self.cookies)} 个字段"

    def to_netscape(self) -> str:
        header = (
            "# Netscape HTTP Cookie File\n"
            "# 由 bili-claw --login 从本机浏览器导出。这是凭据，不要提交到 git。\n"
        )
        return header + "\n".join(cookie.netscape_line() for cookie in self.cookies) + "\n"


@dataclass(frozen=True, slots=True)
class _ChromiumFamily:
    key: str
    label: str
    root: Path
    keychain_service: str
    keychain_account: str


def _browser_support() -> Path:
    return Path.home() / "Library" / "Application Support"


def _chromium_families() -> list[_ChromiumFamily]:
    base = _browser_support()
    return [
        _ChromiumFamily(
            "chrome", "Google Chrome", base / "Google" / "Chrome", "Chrome Safe Storage", "Chrome"
        ),
        _ChromiumFamily(
            "edge", "Microsoft Edge", base / "Microsoft Edge", "Microsoft Edge Safe Storage", "Microsoft Edge"
        ),
        _ChromiumFamily(
            "brave", "Brave", base / "BraveSoftware" / "Brave-Browser", "Brave Safe Storage", "Brave"
        ),
        _ChromiumFamily("chromium", "Chromium", base / "Chromium", "Chromium Safe Storage", "Chromium"),
        _ChromiumFamily("vivaldi", "Vivaldi", base / "Vivaldi", "Vivaldi Safe Storage", "Vivaldi"),
    ]


def _firefox_profiles() -> list[tuple[str, Path]]:
    root = _browser_support() / "Firefox" / "Profiles"
    if not root.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for entry in sorted(root.iterdir()):
        database = entry / "cookies.sqlite"
        if database.is_file():
            found.append((entry.name, database))
    return found


def is_bilibili(host: str) -> bool:
    """域名是否属于 B站。用后缀匹配而不是 `in`，免得 `notbilibili.com` 混进来。"""
    bare = host.lstrip(".").lower()
    return any(bare == suffix or bare.endswith(f".{suffix}") for suffix in DOMAIN_SUFFIXES)


#: 同名 Cookie 在多个域下都出现时，按这个顺序取。
_DOMAIN_RANK = (".bilibili.com", ".bilibili.cn")


def _domain_rank(domain: str) -> int:
    lowered = domain.lower()
    return _DOMAIN_RANK.index(lowered) if lowered in _DOMAIN_RANK else len(_DOMAIN_RANK)


def dedupe(cookies: Iterable[Cookie]) -> tuple[Cookie, ...]:
    """同名 Cookie 只保留一条，让"最终用了哪一个"可复现。

    浏览器里同一个 SESSDATA 会同时挂在 ``.bilibili.com``、``.bilibili.cn`` 和若干子域上。
    而 ``load_from_file`` 把文件读成一个以名字为键的 dict——留着多份的话，真正生效的
    那条取决于 SQLite 的返回顺序，今天对、明天可能就换了。这里显式挑最相关的一份。
    """
    best: dict[str, Cookie] = {}
    for cookie in cookies:
        current = best.get(cookie.name)
        if current is None or _domain_rank(cookie.domain) < _domain_rank(current.domain):
            best[cookie.name] = cookie
    return tuple(sorted(best.values(), key=lambda cookie: cookie.name))


@contextmanager
def _opened(database: Path) -> Iterator[sqlite3.Connection]:
    """复制一份再打开——浏览器持锁时直接读会 `database is locked`。"""
    handle, temporary = tempfile.mkstemp(suffix=".sqlite", prefix="biliclaw-cookies-")
    os.close(handle)
    path = Path(temporary)
    try:
        shutil.copy2(database, path)
        connection = sqlite3.connect(path)
        try:
            yield connection
        finally:
            connection.close()
    finally:
        path.unlink(missing_ok=True)


def _keychain_secret(service: str, account: str) -> bytes:
    """读钥匙串里 Chromium 的加密口令。首次会弹一次系统授权框。"""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service, "-a", account],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BiliError(f"无法执行 security 读取钥匙串：{exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise BiliError(
            f"没能从钥匙串读到「{service}」",
            hint=(
                "首次读取会弹出系统授权框，需要点「允许」；"
                "如果以前点过「拒绝」，打开「钥匙串访问」找到该条目，把访问控制重置后再试"
            ),
        )
    return result.stdout.strip().encode()


def _decrypt(key: bytes, blob: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise MissingDependencyError(
            "解密 Chromium 系的 Cookie 需要 cryptography",
            hint="pip install cryptography，或重装：uv tool install --force bili-claw",
        ) from exc
    decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
    return decryptor.update(blob) + decryptor.finalize()


def decrypt_value(blob: bytes, key: bytes, host: str) -> str | None:
    """把 Chromium 的 `encrypted_value` 还原成明文；认不出来时返回 None。"""
    if not blob:
        return None
    prefix, payload = blob[:3], blob[3:]
    if prefix not in (b"v10", b"v11"):
        # v20 是 Windows 的 App-Bound 加密，macOS 上不该出现
        log.debug("跳过不认识的前缀 %r（host=%s）", prefix, host)
        return None
    raw = _decrypt(key, payload)
    if raw:
        padding = raw[-1]
        if 0 < padding <= 16:
            raw = raw[:-padding]
    if len(raw) >= _DOMAIN_HASH_LENGTH and raw[:_DOMAIN_HASH_LENGTH] == hashlib.sha256(host.encode()).digest():
        raw = raw[_DOMAIN_HASH_LENGTH:]
    return raw.decode("utf-8", "replace")


def chrome_time_to_unix(value: int | None) -> int:
    """Chromium 的微秒时间戳 → Unix 秒；0 或缺失表示会话 Cookie。"""
    if not value:
        return 0
    return max(0, int(value / 1_000_000) - _CHROME_EPOCH_OFFSET)


def _pick_database(root: Path) -> Path | None:
    """同一个浏览器可能有多个 profile，挑 Cookie 库最近改动过的那个。"""
    candidates = [root / "Default" / "Cookies"]
    candidates.extend(sorted(root.glob("Profile */Cookies")))
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def _read_chromium(family: _ChromiumFamily) -> Extraction | None:
    database = _pick_database(family.root)
    if database is None:
        return None
    key = hashlib.pbkdf2_hmac("sha1", _keychain_secret(family.keychain_service, family.keychain_account),
                              _SALT, _ITERATIONS, _KEY_LENGTH)
    cookies: list[Cookie] = []
    with _opened(database) as connection:
        rows = connection.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure FROM cookies"
        ).fetchall()
    for host, name, plain, blob, path, expires, secure in rows:
        if not is_bilibili(host):
            continue
        value = plain or decrypt_value(blob, key, host)
        if not value:
            continue
        cookies.append(
            Cookie(
                name=name,
                value=value,
                domain=host,
                path=path or "/",
                secure=bool(secure),
                expires=chrome_time_to_unix(expires),
            )
        )
    if not cookies:
        return None
    return Extraction(family.label, database.parent.name, dedupe(cookies))


def _read_firefox(name: str, database: Path) -> Extraction | None:
    cookies: list[Cookie] = []
    with _opened(database) as connection:
        rows = connection.execute(
            "SELECT host, name, value, path, expiry, isSecure FROM moz_cookies"
        ).fetchall()
    for host, cookie_name, value, path, expiry, secure in rows:
        if not is_bilibili(host) or not value:
            continue
        cookies.append(
            Cookie(
                name=cookie_name,
                value=value,
                domain=host,
                path=path or "/",
                secure=bool(secure),
                expires=int(expiry or 0),
            )
        )
    if not cookies:
        return None
    return Extraction("Firefox", name, dedupe(cookies))


#: `--browser` 接受的名字。`auto` 按下面的顺序依次尝试。
BROWSERS = ("auto", "chrome", "edge", "brave", "chromium", "vivaldi", "firefox", "safari")

_SAFARI_HINT = (
    "Safari 的 Cookie 是私有二进制格式，而且要先在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」"
    "里授权终端，所以没做支持；请改用 Chrome 或 Firefox，或用 --cookie-file 手工提供"
)


def _installed_chromium() -> list[_ChromiumFamily]:
    return [family for family in _chromium_families() if family.root.is_dir()]


def extract(browser: str = "auto", *, profile: str | None = None) -> Extraction:
    """从本机浏览器取出 B站的 Cookie。

    找不到任何登录态时抛 `UsageError`，并把「查过哪些浏览器、各自为什么没成」写进提示里
    ——用户需要知道是该去登录，还是该换个浏览器。

    优先级固定为 Chrome → Edge → Brave → Chromium → Vivaldi → Firefox，而不是"谁的库最新"：
    固定顺序至少是可预测的，真取错了 ``--browser`` 一指定就能绕开。
    """
    if browser == "safari":
        raise UsageError("暂不支持从 Safari 读取 Cookie", hint=_SAFARI_HINT)
    if browser not in BROWSERS:
        raise UsageError(f"不认识的浏览器：{browser}", hint=f"可选：{'、'.join(BROWSERS)}")

    attempts: list[str] = []
    families = _installed_chromium()
    firefox_entries = _firefox_profiles()
    if profile is not None:
        firefox_entries = [entry for entry in firefox_entries if entry[0] == profile]

    if browser not in ("auto", "firefox"):
        family = next((item for item in _chromium_families() if item.key == browser), None)
        if family is not None and family not in families:
            # 指定了某个浏览器、而它连 profile 目录都没有，先说清楚，
            # 免得落到"浏览器里没有 B站的 Cookie"这个会让人去重新登录的错误结论上
            raise UsageError(
                f"没找到 {family.label} 的 profile 目录",
                hint="确认它装过并至少启动过一次；也可以换 --browser，或用 --cookie-file 手工提供",
            )

    for family in families:
        if browser not in ("auto", family.key):
            continue
        if (found := _read_chromium(family)) is not None:
            return found
        attempts.append(f"{family.label}：装了，但库里没有 B站的 Cookie")

    if browser in ("auto", "firefox"):
        for name, database in firefox_entries:
            if (found := _read_firefox(name, database)) is not None:
                return found
            attempts.append(f"Firefox（{name}）：库里没有 B站的 Cookie")
        if not firefox_entries:
            attempts.append("Firefox：没找到 profile")

    if not families and not firefox_entries:
        raise UsageError(
            "没找到受支持的浏览器",
            hint=(
                "查过 Chrome / Edge / Brave / Chromium / Vivaldi / Firefox 的 profile 目录。"
                "要么装一个并登录 B站，要么用 --cookie-file 手工提供"
            ),
        )
    raise UsageError(
        "浏览器里没有 B站的 Cookie",
        hint=(
            "先用那个浏览器打开 bilibili.com 并确认已经登录，再跑一次 --login。\n  "
            + "\n  ".join(attempts or ["（没有可用的 profile）"])
        ),
    )


def save(extraction: Extraction, path: Path) -> Path:
    """把提取结果写成 0600 的 Netscape cookies.txt。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先建再写：umask 可能是 022，靠 open() 的默认权限会留下一个短暂的可读窗口
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(extraction.to_netscape())
    path.chmod(0o600)
    return path
