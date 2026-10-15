"""一键从本机浏览器取出 B站登录态。

这是 FR-09 的便捷入口：与其让用户去开发者工具里翻 Request Headers，不如直接读
浏览器自己的 Cookie 库。三条硬规矩：

1. **Cookie 的值永不打印**。本模块对外只回报「取到了几个字段」，值只在内存里过一手，
   最终由调用方写进 0600 的文件。
2. **绝不改动浏览器自己的库**。浏览器运行时 SQLite 是锁着的，所以一律先复制到临时
   文件再读，读完即删。
3. **取不到要说清是哪种取不到**。没装受支持的浏览器、装了但没登录过 B站、钥匙串授权
   被拒——是三种完全不同的处境，提示也必须不同，否则用户会朝错误的方向排查。

## 各平台支持矩阵

| | Chromium 系（Chrome / Edge / Brave / Chromium / Vivaldi） | Firefox |
|---|---|---|
| macOS | ✅ 钥匙串口令 → PBKDF2(1003) → AES-128-CBC | ✅ 明文 |
| Linux | ✅ gnome-keyring 口令，没接上就用 `peanuts` → PBKDF2(1) | ✅ 明文 |
| Windows | ⚠️ **仅 `v10`**：Local State 的 DPAPI 密钥 → AES-256-GCM | ✅ 明文 |

**Windows 上 Chrome 127+ 的 `v20`（App-Bound Encryption）明确不支持。** 那套方案把
密钥交给一个受保护的系统服务，正规解法只有「从 Chrome 进程里读内存」这类做法——既不
稳定，也和恶意软件的手法没有区别，本工具不做。遇到时给明确提示，并指向 Firefox 与
`--cookie-file` 两条出路。

**Firefox 是三个平台上投入产出比最高的一条路**：它的 `cookies.sqlite` **三平台都是明文**，
没有加密、没有钥匙串、没有 DPAPI，差别只在 profile 目录的位置。

Safari 的 Cookie 是私有二进制格式且需要「完全磁盘访问权限」，不做支持，但会给出明确
提示而不是静默失败。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
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
_ITERATIONS = 1003  # macOS 的迭代次数
_KEY_LENGTH = 16  # AES-128
_IV = b" " * 16  # 16 个空格

#: Linux 的迭代次数是 **1**，不是 macOS 的 1003。同一个 PBKDF2、同一个 salt，
#: 只有这个数字不同——照抄 macOS 的 1003 会解出一堆乱码而不是报错。
_LINUX_ITERATIONS = 1

#: Chromium 在 Linux 上没接上钥匙串时用的固定口令，硬编码在源码里。
_LINUX_FALLBACK_PASSWORD = b"peanuts"

#: Chrome 130 起在明文前面拼了一段 SHA256(host_key)，把 Cookie 绑到域名上。
_DOMAIN_HASH_LENGTH = 32

#: Windows 的 AES-256-GCM：12 字节 nonce + 密文 + 16 字节 tag。
_GCM_NONCE_LENGTH = 12
_GCM_TAG_LENGTH = 16

#: Windows 上被 DPAPI 包起来的密钥带这个前缀。
_DPAPI_PREFIX = b"DPAPI"

#: Chrome 127+ 的 App-Bound 加密前缀。明确不支持（见模块自述）。
_APP_BOUND_PREFIX = b"v20"

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


@dataclass(frozen=True, slots=True)
class _Vendor:
    """一个 Chromium 系浏览器在三个平台上的位置。

    写成一张表而不是三串 if，是因为「哪个平台放在哪」正是这里唯一要回答的问题，
    摊平了才看得清三者其实**互不相同**：macOS 是 `Google/Chrome`，Windows 还要多
    一层 `User Data`，Linux 则是 `~/.config` 下的扁平名字。照着 macOS 的样子去猜
    Windows 的路径，会得到「找不到 profile」——而那个提示会把人引去重新登录。
    """

    key: str
    label: str
    darwin: str
    win32: str
    linux: str
    keychain_service: str
    keychain_account: str
    #: gnome-keyring 里那条记录的 application 属性
    keyring: str


_VENDORS = (
    _Vendor(
        "chrome", "Google Chrome",
        "Google/Chrome", "Google/Chrome/User Data", "google-chrome",
        "Chrome Safe Storage", "Chrome", "chrome",
    ),
    _Vendor(
        "edge", "Microsoft Edge",
        "Microsoft Edge", "Microsoft/Edge/User Data", "microsoft-edge",
        "Microsoft Edge Safe Storage", "Microsoft Edge", "microsoft-edge",
    ),
    _Vendor(
        "brave", "Brave",
        "BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser/User Data", "BraveSoftware/Brave-Browser",
        "Brave Safe Storage", "Brave", "brave",
    ),
    _Vendor(
        "chromium", "Chromium",
        "Chromium", "Chromium/User Data", "chromium",
        "Chromium Safe Storage", "Chromium", "chromium",
    ),
    _Vendor(
        "vivaldi", "Vivaldi",
        "Vivaldi", "Vivaldi/User Data", "vivaldi",
        "Vivaldi Safe Storage", "Vivaldi", "vivaldi",
    ),
)


def _platform() -> str:
    """归一成 ``darwin`` / ``win32`` / ``linux`` 三者之一。

    其余一切都按这三档分派，所以「这个平台算什么」只能有一个答案——散落的
    `sys.platform == ...` 判断迟早会出现两种口径，而那种 bug 只在别人的机器上复现。
    """
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    return "linux"


def _chromium_base() -> Path:
    """Chromium 系 profile 的基准目录（各浏览器的相对路径见 `_VENDORS`）。"""
    platform = _platform()
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if platform == "win32":
        # 环境变量缺失时退回约定位置，而不是直接用 ~ —— 在 Windows 上
        # 真正的 profile 从不放在用户主目录里，猜错等于静默什么都找不到。
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _chromium_families() -> list[_ChromiumFamily]:
    base = _chromium_base()
    platform = _platform()
    return [
        _ChromiumFamily(
            vendor.key,
            vendor.label,
            base / getattr(vendor, platform),
            vendor.keychain_service,
            vendor.keychain_account,
        )
        for vendor in _VENDORS
    ]


def _keyring_application(family: _ChromiumFamily) -> str:
    """Linux 的 gnome-keyring 用 application 属性区分浏览器。"""
    for vendor in _VENDORS:
        if vendor.key == family.key:
            return vendor.keyring
    return family.key


def _firefox_roots() -> list[Path]:
    """Firefox 的 profile 目录。三个平台又各不相同，Linux 还不止一处。"""
    platform = _platform()
    if platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"]
    if platform == "win32":
        # Firefox 在 Windows 上用的是 Roaming，不是 Local
        appdata = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return [Path(appdata) / "Mozilla" / "Firefox" / "Profiles"]
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "mozilla" / "firefox"
    # 原生安装把 profile 直接放在 firefox/ 下；部分发行版与 Flatpak 会再包一层 Profiles/
    return [base, base / "Profiles"]


def _firefox_profiles() -> list[tuple[str, Path]]:
    found: dict[str, Path] = {}
    for root in _firefox_roots():
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            database = entry / "cookies.sqlite"
            if database.is_file():
                found.setdefault(entry.name, database)
    return sorted(found.items())


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


def _keyring_secret(application: str) -> bytes | None:
    """Linux：从 gnome-keyring 里取口令；没装、或没这条记录时返回 None。

    走 `secret-tool` 这个命令行前端而不是直接调 D-Bus：少一个依赖，而且用户自己也能
    用同一条命令验证「钥匙串里到底有没有这条记录」——排查的时候这一点很值钱。
    """
    if shutil.which("secret-tool") is None:
        return None
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", application],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("secret-tool 调用失败：%s", exc)
        return None
    password = result.stdout.strip()
    if result.returncode != 0 or not password:
        return None
    return password.encode()


def _dpapi_unprotect(blob: bytes) -> bytes:
    """Windows：调 `CryptUnprotectData`。用 ctypes 直接调，不引入新依赖。"""
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(blob, len(blob))
    incoming = _DataBlob(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    outgoing = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)
    ):
        raise BiliError(
            "DPAPI 解不开 Local State 里的密钥",
            hint="这份配置多半是另一个 Windows 账户建的，当前用户解不开；请换 --browser，或用 --cookie-file 手工提供",
        )
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.pbData)


def _windows_key(root: Path) -> bytes:
    """Windows：取出被 DPAPI 保护的 AES-256 密钥。

    Windows 的方案与 macOS/Linux **不是一回事**：不派生口令，而是在 `Local State` 里
    放一个 32 字节随机密钥，外面套一层 DPAPI——只有同一个 Windows 用户解得开。所以
    这里既没有 PBKDF2，也不该照抄 macOS 的参数。
    """
    state = root / "Local State"
    try:
        payload = json.loads(state.read_text(encoding="utf-8"))
        blob = base64.b64decode(payload["os_crypt"]["encrypted_key"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BiliError(
            f"没能从 {state} 读到 os_crypt.encrypted_key",
            hint="确认这个浏览器至少启动过一次；也可以换 --browser，或用 --cookie-file 手工提供",
        ) from exc
    if not blob.startswith(_DPAPI_PREFIX):
        raise BiliError(
            "Local State 里的密钥没有 DPAPI 前缀，格式与预期不符",
            hint="多半是这个 Chromium 版本换了方案；请换 --browser，或用 --cookie-file 手工提供",
        )
    return _dpapi_unprotect(blob[len(_DPAPI_PREFIX) :])


def _chromium_key(family: _ChromiumFamily) -> bytes:
    """本平台上这个浏览器解密 Cookie 用的密钥。

    三个平台的方案**互不相同**，不是同一个东西换个名字：

    - **macOS**：钥匙串口令 → ``PBKDF2(saltysalt, 1003)`` → 16 字节 AES-128 密钥
    - **Linux**：钥匙串口令（没接上就用 ``peanuts``）→ ``PBKDF2(saltysalt, 1)``
    - **Windows**：``Local State`` 里被 DPAPI 保护的 32 字节 AES-256 密钥

    中间那个迭代次数是最容易抄错的地方：Linux 是 **1**，照抄 macOS 的 1003 不会
    报错，只会解出一堆乱码。
    """
    platform = _platform()
    if platform == "darwin":
        password = _keychain_secret(family.keychain_service, family.keychain_account)
        return hashlib.pbkdf2_hmac("sha1", password, _SALT, _ITERATIONS, _KEY_LENGTH)
    if platform == "win32":
        return _windows_key(family.root)
    password = _keyring_secret(_keyring_application(family)) or _LINUX_FALLBACK_PASSWORD
    return hashlib.pbkdf2_hmac("sha1", password, _SALT, _LINUX_ITERATIONS, _KEY_LENGTH)


def _cryptography():
    """惰性导入 cryptography 的密码学原语。

    放在函数里而不是模块顶层：只用下载功能的人不该为 `--login` 付这份导入开销，
    而且没装 cryptography 时也只有这条路径会给出「照做就能好」的提示。

    （返回的是 `(Cipher, algorithms, modes)` 三个模块属性；这里不写返回注解，
    因为要写全得先把 cryptography 的类型搬进来，反而绕远了。）
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise MissingDependencyError(
            "解密 Chromium 系的 Cookie 需要 cryptography",
            hint="pip install cryptography，或重装：uv tool install --force bili-claw",
        ) from exc
    return Cipher, algorithms, modes


def _decrypt(key: bytes, blob: bytes) -> bytes:
    """macOS / Linux：AES-128-CBC，IV 是 16 个空格。"""
    Cipher, algorithms, modes = _cryptography()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
    return decryptor.update(blob) + decryptor.finalize()


def _decrypt_gcm(key: bytes, blob: bytes) -> bytes | None:
    """Windows：AES-256-GCM。12 字节 nonce 在前，16 字节 tag 在后，没有填充可剥。

    解不开（tag 校验不过）时返回 None 而不是抛异常：一条 Cookie 解不出来不该让整次
    提取失败，它多半只是属于另一个 Windows 账户。
    """
    from cryptography.exceptions import InvalidTag

    Cipher, algorithms, modes = _cryptography()
    if len(blob) <= _GCM_NONCE_LENGTH + _GCM_TAG_LENGTH:
        return None
    nonce = blob[:_GCM_NONCE_LENGTH]
    payload = blob[_GCM_NONCE_LENGTH : -_GCM_TAG_LENGTH]
    tag = blob[-_GCM_TAG_LENGTH:]
    try:
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    except ValueError as exc:  # pragma: no cover - 密钥长度不对才会走到
        log.debug("GCM 参数不合法：%s", exc)
        return None
    try:
        return decryptor.update(payload) + decryptor.finalize()
    except InvalidTag:
        return None


def _strip_domain_hash(raw: bytes, host: str) -> bytes:
    """Chrome 130 起在明文前拼了 32 字节 ``SHA256(host_key)``，要剥掉。

    对不上不算错误：那要么是更早的 Chrome 写的（根本没有这段前缀），要么 host 与
    写入时不同。两种情况都该原样返回。
    """
    digest = hashlib.sha256(host.encode()).digest()
    if len(raw) >= _DOMAIN_HASH_LENGTH and raw[:_DOMAIN_HASH_LENGTH] == digest:
        return raw[_DOMAIN_HASH_LENGTH:]
    return raw


def decrypt_value(blob: bytes, key: bytes, host: str) -> str | None:
    """macOS / Linux：把 `encrypted_value` 还原成明文；认不出来时返回 None。"""
    if not blob:
        return None
    prefix, payload = blob[:3], blob[3:]
    if prefix not in (b"v10", b"v11"):
        log.debug("跳过不认识的前缀 %r（host=%s）", prefix, host)
        return None
    raw = _decrypt(key, payload)
    if raw:
        padding = raw[-1]
        if 0 < padding <= 16:
            raw = raw[:-padding]
    return _strip_domain_hash(raw, host).decode("utf-8", "replace")


def decrypt_value_gcm(blob: bytes, key: bytes, host: str) -> str | None:
    """Windows：同样的 `v10` 前缀，但里面是 AES-256-GCM（Windows 从 Chrome 80 起）。"""
    if not blob or blob[:3] != b"v10":
        return None
    raw = _decrypt_gcm(key, blob[3:])
    if raw is None:
        return None
    return _strip_domain_hash(raw, host).decode("utf-8", "replace")


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


class _AppBound(Exception):
    """这个 Chromium 的 Cookie 用了 App-Bound 加密，本程序解不开。

    单独一个异常类型，是为了让 `extract` 能说清「它登录过，只是我们解不开」——
    这两种处境给用户的指令完全相反：一个叫他去登录（白费功夫），一个叫他换浏览器。
    """


def _read_chromium(family: _ChromiumFamily) -> Extraction | None:
    database = _pick_database(family.root)
    if database is None:
        return None
    key = _chromium_key(family)
    decrypt = decrypt_value_gcm if _platform() == "win32" else decrypt_value
    cookies: list[Cookie] = []
    app_bound = False
    with _opened(database) as connection:
        rows = connection.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure FROM cookies"
        ).fetchall()
    for host, name, plain, blob, path, expires, secure in rows:
        if not is_bilibili(host):
            continue
        if blob[:3] == _APP_BOUND_PREFIX:
            # Chrome 127+ 在 Windows 上把密钥又套了一层，只有它自己的进程拿得到。
            # 记一笔，等确认一条都没解出来时再据此给出准确的提示。
            app_bound = True
            continue
        value = plain or decrypt(blob, key, host)
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
        if app_bound:
            raise _AppBound
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


def _safari_hint() -> str:
    """Safari 提示。非 macOS 上要说得直白些，免得让人以为自己少装了什么。"""
    if _platform() != "darwin":
        return "Safari 只有 macOS 上才有；本平台请改用 Firefox，或用 --cookie-file 手工提供"
    return (
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
        raise UsageError("暂不支持从 Safari 读取 Cookie", hint=_safari_hint())
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
        try:
            found = _read_chromium(family)
        except _AppBound:
            attempts.append(f"{family.label}：Cookie 用了 App-Bound 加密，本程序解不开（它是登录过的）")
            continue
        if found is not None:
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
