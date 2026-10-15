"""`browser.py` 的测试。

解密那条路径没法拿真实浏览器做回归：跑测试的机器上未必有钥匙串，更不该去碰用户的
真实凭据。所以这里用**自己加密的夹具**——测试里拿同一套参数把明文压成 v10 密文，
再让实现解回来。Chromium 哪天换了加密参数，往返测试会立刻变红。

同理，浏览器 profile 全部用临时目录里造的合成 SQLite，不读 `~/Library`。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from biliclaw import browser
from biliclaw.cookies import load_from_file
from biliclaw.errors import BiliError, UsageError

#: 固定的测试密钥，与真实钥匙串无关。
KEY = bytes(range(16))
IV = b" " * 16

#: Chromium 写密文时会把 SHA256(host_key) 拼在明文前面，用来把 Cookie 绑到域名上。
BOUND_HOST = ".bilibili.com"


def encrypt(
    plaintext: str,
    *,
    key: bytes = KEY,
    host: str = BOUND_HOST,
    prefix: bytes = b"v10",
    bind: bool = True,
) -> bytes:
    """造一份与 Chromium 等价的密文。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = plaintext.encode()
    if bind:
        raw = hashlib.sha256(host.encode()).digest() + raw
    padding = 16 - len(raw) % 16
    raw += bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    return prefix + encryptor.update(raw) + encryptor.finalize()


# --------------------------------------------------------------------------- #
# 解密
# --------------------------------------------------------------------------- #


def test_decrypt_round_trips_a_bound_value() -> None:
    blob = encrypt("SESSDATA-VALUE", host=BOUND_HOST)
    assert browser.decrypt_value(blob, KEY, BOUND_HOST) == "SESSDATA-VALUE"


def test_decrypt_round_trips_an_unbound_value() -> None:
    """老版本 Chromium 不加域名哈希，也不能解坏。"""
    blob = encrypt("plain-value", bind=False)
    assert browser.decrypt_value(blob, KEY, BOUND_HOST) == "plain-value"


def test_decrypt_only_strips_the_hash_of_the_matching_host() -> None:
    """域名对不上时那份哈希必须留着——它正是用来发现"这条 Cookie 不属于这个域"的。"""
    blob = encrypt("secret", host=BOUND_HOST)
    assert browser.decrypt_value(blob, KEY, BOUND_HOST) == "secret"

    other = browser.decrypt_value(blob, KEY, ".example.com")
    assert other is not None
    assert other != "secret"
    # 明文还在，只是前面多了 32 字节哈希（它们解出来是乱码，长度不能按字符数算）
    assert other.endswith("secret")
    assert len(other) > len("secret")


def test_decrypt_handles_unicode_values() -> None:
    blob = encrypt("你好，世界", host=BOUND_HOST)
    assert browser.decrypt_value(blob, KEY, BOUND_HOST) == "你好，世界"


def test_decrypt_returns_none_for_an_empty_blob() -> None:
    assert browser.decrypt_value(b"", KEY, BOUND_HOST) is None


def test_decrypt_returns_none_for_an_unknown_prefix() -> None:
    """v20 是 Windows 的 App-Bound 加密，认不出来时要返回 None 而不是抛异常。"""
    blob = b"v20" + encrypt("x", bind=False)[3:]
    assert browser.decrypt_value(blob, KEY, BOUND_HOST) is None


# --------------------------------------------------------------------------- #
# 时间戳与域名
# --------------------------------------------------------------------------- #


def test_chrome_time_converts_to_unix() -> None:
    # 2024-01-01 00:00:00 UTC 的 Chromium 时间戳
    assert browser.chrome_time_to_unix(13_348_540_800_000_000) == 1_704_067_200


def test_chrome_time_zero_means_a_session_cookie() -> None:
    assert browser.chrome_time_to_unix(0) == 0
    assert browser.chrome_time_to_unix(None) == 0


@pytest.mark.parametrize(
    "host", [".bilibili.com", "bilibili.com", "www.bilibili.com", ".bilibili.cn", "live.bilibili.com"]
)
def test_is_bilibili_accepts_real_domains(host: str) -> None:
    assert browser.is_bilibili(host)


@pytest.mark.parametrize("host", ["notbilibili.com", "bilibili.com.evil.net", "example.com", ""])
def test_is_bilibili_rejects_lookalikes(host: str) -> None:
    """用后缀匹配而不是子串匹配：`notbilibili.com` 不是 B站。"""
    assert not browser.is_bilibili(host)


# --------------------------------------------------------------------------- #
# 去重与序列化
# --------------------------------------------------------------------------- #


def cookie(name: str, value: str, domain: str = BOUND_HOST) -> browser.Cookie:
    return browser.Cookie(name=name, value=value, domain=domain)


def test_dedupe_prefers_the_canonical_domain() -> None:
    """同名 Cookie 挂在多个域下时，取 `.bilibili.com` 那份——api.bilibili.com 只认得到它。"""
    kept = browser.dedupe(
        [cookie("SESSDATA", "cn", ".bilibili.cn"), cookie("SESSDATA", "com", ".bilibili.com")]
    )
    assert [(item.name, item.value) for item in kept] == [("SESSDATA", "com")]


def test_dedupe_prefers_canonical_over_a_subdomain() -> None:
    kept = browser.dedupe(
        [cookie("bili_jct", "sub", "www.bilibili.com"), cookie("bili_jct", "top", ".bilibili.com")]
    )
    assert [item.value for item in kept] == ["top"]


def test_dedupe_is_order_independent() -> None:
    """无论 SQLite 先返回哪一条，结果都得一样——否则今天的登录态明天就换了个人。"""
    forward = browser.dedupe(
        [cookie("SESSDATA", "com", ".bilibili.com"), cookie("SESSDATA", "cn", ".bilibili.cn")]
    )
    backward = browser.dedupe(
        [cookie("SESSDATA", "cn", ".bilibili.cn"), cookie("SESSDATA", "com", ".bilibili.com")]
    )
    assert forward == backward


def test_dedupe_sorts_by_name_for_a_stable_file() -> None:
    kept = browser.dedupe([cookie("z", "1"), cookie("a", "2"), cookie("m", "3")])
    assert [item.name for item in kept] == ["a", "m", "z"]


def test_netscape_line_marks_a_leading_dot_as_a_domain_cookie() -> None:
    line = cookie("SESSDATA", "v", ".bilibili.com").netscape_line().split("\t")
    assert line[0] == ".bilibili.com"
    assert line[1] == "TRUE"  # 前导点 = 域 Cookie
    assert line[5] == "SESSDATA"
    assert line[6] == "v"

    host_only = cookie("SESSDATA", "v", "www.bilibili.com").netscape_line().split("\t")
    assert host_only[1] == "FALSE"


def test_saved_file_can_be_read_back_by_the_cookie_loader(tmp_path: Path) -> None:
    """写出来的必须是自己读得进去的格式——这条把导出与导入扣在一起。"""
    extraction = browser.Extraction(
        "Chrome",
        "Default",
        (cookie("SESSDATA", "abc"), cookie("bili_jct", "def", ".bilibili.cn")),
    )
    path = browser.save(extraction, tmp_path / "nested" / "cookies.txt")
    assert load_from_file(path) == {"SESSDATA": "abc", "bili_jct": "def"}


def test_save_writes_a_private_file(tmp_path: Path) -> None:
    path = browser.save(
        browser.Extraction("Chrome", "Default", (cookie("SESSDATA", "abc"),)), tmp_path / "c.txt"
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_save_tightens_an_existing_permissive_file(tmp_path: Path) -> None:
    """覆盖一个本来就存在、权限过宽的文件时，必须顺手把权限收回来。"""
    path = tmp_path / "c.txt"
    path.write_text("旧内容")
    path.chmod(0o644)
    browser.save(browser.Extraction("Chrome", "Default", (cookie("SESSDATA", "abc"),)), path)
    assert path.stat().st_mode & 0o777 == 0o600


# --------------------------------------------------------------------------- #
# 读合成 profile
# --------------------------------------------------------------------------- #


def make_firefox(directory: Path, rows: list[tuple[str, str, str]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "cookies.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE moz_cookies(host TEXT, name TEXT, value TEXT, path TEXT,"
            " expiry INTEGER, isSecure INTEGER)"
        )
        connection.executemany(
            "INSERT INTO moz_cookies VALUES (?, ?, ?, '/', 0, 1)", rows
        )
    return database


def make_chromium(root: Path, rows: list[tuple[str, str, bytes]], *, value: str = "") -> Path:
    """造一个 Chromium 的 Cookies 库。`rows` 是 (host, name, encrypted_value)。"""
    profile = root / "Default"
    profile.mkdir(parents=True, exist_ok=True)
    database = profile / "Cookies"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE cookies(host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB,"
            " path TEXT, expires_utc INTEGER, is_secure INTEGER)"
        )
        connection.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, '/', 0, 1)",
            [(host, name, value, blob) for host, name, blob in rows],
        )
    return database


def family(root: Path, key: str = "chrome", label: str = "Google Chrome") -> browser._ChromiumFamily:
    return browser._ChromiumFamily(key, label, root, "Chrome Safe Storage", "Chrome")


def test_read_firefox_picks_up_bilibili_cookies(tmp_path: Path) -> None:
    database = make_firefox(
        tmp_path / "abc.default",
        [(".bilibili.com", "SESSDATA", "ff-value"), ("example.com", "tracker", "nope")],
    )
    found = browser._read_firefox("abc.default", database)
    assert found is not None
    assert found.mapping == {"SESSDATA": "ff-value"}
    assert found.browser == "Firefox"


def test_read_firefox_returns_none_when_there_is_nothing_for_bilibili(tmp_path: Path) -> None:
    database = make_firefox(tmp_path / "abc.default", [("example.com", "tracker", "nope")])
    assert browser._read_firefox("abc.default", database) is None


def test_read_chromium_decrypts_with_the_keychain_key(tmp_path: Path, monkeypatch) -> None:
    """密钥走 PBKDF2 派生，测试里固定口令，避免碰真实钥匙串。"""
    monkeypatch.setattr(browser, "_keychain_secret", lambda service, account: b"peanuts")
    key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1003, 16)
    make_chromium(
        tmp_path,
        [
            (".bilibili.com", "SESSDATA", encrypt("chrome-value", key=key)),
            (".bilibili.com", "bili_jct", encrypt("csrf", key=key)),
            ("example.com", "tracker", encrypt("nope", key=key, host="example.com")),
        ],
    )
    found = browser._read_chromium(family(tmp_path))
    assert found is not None
    assert found.mapping == {"SESSDATA": "chrome-value", "bili_jct": "csrf"}
    assert found.profile == "Default"


def test_read_chromium_falls_back_to_the_plain_value_column(tmp_path: Path, monkeypatch) -> None:
    """有些 Cookie 是明文存在 value 列里的，不能因为解密不出来就丢掉。"""
    monkeypatch.setattr(browser, "_keychain_secret", lambda service, account: b"peanuts")
    make_chromium(tmp_path, [(".bilibili.com", "b_nut", b"")], value="1700000000")
    found = browser._read_chromium(family(tmp_path))
    assert found is not None
    assert found.mapping == {"b_nut": "1700000000"}


def test_read_chromium_returns_none_when_there_is_nothing_for_bilibili(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(browser, "_keychain_secret", lambda service, account: b"peanuts")
    make_chromium(tmp_path, [("example.com", "tracker", b"")], value="x")
    assert browser._read_chromium(family(tmp_path)) is None


def test_read_chromium_returns_none_when_the_profile_does_not_exist(tmp_path: Path) -> None:
    """profile 目录都没有就不该去动钥匙串——否则会平白弹一个授权框。"""
    called = False

    def boom(service: str, account: str) -> bytes:
        nonlocal called
        called = True
        return b"peanuts"

    browser._keychain_secret, original = boom, browser._keychain_secret
    try:
        assert browser._read_chromium(family(tmp_path)) is None
    finally:
        browser._keychain_secret = original
    assert not called


# --------------------------------------------------------------------------- #
# extract 的分支与提示
# --------------------------------------------------------------------------- #


def test_extract_refuses_safari_with_a_reason() -> None:
    with pytest.raises(UsageError) as info:
        browser.extract("safari")
    assert "完全磁盘访问权限" in (info.value.hint or "")


def test_extract_rejects_an_unknown_browser() -> None:
    with pytest.raises(UsageError) as info:
        browser.extract("netscape")
    assert "chrome" in (info.value.hint or "")


def test_extract_reports_no_supported_browser(monkeypatch) -> None:
    monkeypatch.setattr(browser, "_chromium_families", list)
    monkeypatch.setattr(browser, "_firefox_profiles", list)
    with pytest.raises(UsageError) as info:
        browser.extract()
    assert "没找到受支持的浏览器" in info.value.message


def test_extract_tells_the_user_to_log_in_when_no_cookies_are_found(
    tmp_path: Path, monkeypatch
) -> None:
    """装了浏览器但库里没有 B站的 Cookie——这时该说的是去登录，而不是"没装浏览器"。"""
    make_firefox(tmp_path / "abc.default", [("example.com", "tracker", "nope")])
    database = tmp_path / "abc.default" / "cookies.sqlite"
    monkeypatch.setattr(browser, "_chromium_families", list)
    monkeypatch.setattr(browser, "_firefox_profiles", lambda: [("abc.default", database)])
    with pytest.raises(UsageError) as info:
        browser.extract()
    assert "bilibili.com" in (info.value.hint or "")
    assert "Firefox（abc.default）" in (info.value.hint or "")


def test_extract_says_so_when_the_named_browser_is_not_installed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(browser, "_chromium_families", lambda: [family(tmp_path / "missing", "chrome")])
    monkeypatch.setattr(browser, "_firefox_profiles", list)
    with pytest.raises(UsageError) as info:
        browser.extract("chrome")
    assert "profile 目录" in info.value.message


def test_extract_prefers_chromium_over_firefox(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(browser, "_keychain_secret", lambda service, account: b"peanuts")
    key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1003, 16)
    make_chromium(tmp_path / "chrome", [(".bilibili.com", "SESSDATA", encrypt("from-chrome", key=key))])
    firefox_db = make_firefox(tmp_path / "ff", [(".bilibili.com", "SESSDATA", "from-firefox")])
    monkeypatch.setattr(browser, "_chromium_families", lambda: [family(tmp_path / "chrome")])
    monkeypatch.setattr(browser, "_firefox_profiles", lambda: [("ff", firefox_db)])
    assert browser.extract().mapping["SESSDATA"] == "from-chrome"


def test_extract_falls_back_to_firefox_when_chromium_has_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """Chrome 装了但没登录过 B站时，不该就此放弃——Firefox 里可能有。"""
    monkeypatch.setattr(browser, "_keychain_secret", lambda service, account: b"peanuts")
    make_chromium(tmp_path / "chrome", [("example.com", "tracker", b"")], value="x")
    firefox_db = make_firefox(tmp_path / "ff", [(".bilibili.com", "SESSDATA", "from-firefox")])
    monkeypatch.setattr(browser, "_chromium_families", lambda: [family(tmp_path / "chrome")])
    monkeypatch.setattr(browser, "_firefox_profiles", lambda: [("ff", firefox_db)])
    assert browser.extract().mapping["SESSDATA"] == "from-firefox"


def test_extract_browser_flag_restricts_the_search(tmp_path: Path, monkeypatch) -> None:
    """`--browser firefox` 时不该去读 Chrome，哪怕 Chrome 里有更新鲜的登录态。"""
    monkeypatch.setattr(browser, "_keychain_secret", lambda service, account: b"peanuts")
    key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1003, 16)
    make_chromium(tmp_path / "chrome", [(".bilibili.com", "SESSDATA", encrypt("from-chrome", key=key))])
    firefox_db = make_firefox(tmp_path / "ff", [(".bilibili.com", "SESSDATA", "from-firefox")])
    monkeypatch.setattr(browser, "_chromium_families", lambda: [family(tmp_path / "chrome")])
    monkeypatch.setattr(browser, "_firefox_profiles", lambda: [("ff", firefox_db)])
    assert browser.extract("firefox").mapping["SESSDATA"] == "from-firefox"


# --------------------------------------------------------------------------- #
# 三个平台的密钥与目录
# --------------------------------------------------------------------------- #


def on(monkeypatch, platform: str) -> None:
    monkeypatch.setattr(browser, "_platform", lambda: platform)


def test_linux_derives_with_one_iteration_not_a_thousand(monkeypatch) -> None:
    """Linux 的迭代次数是 1。照抄 macOS 的 1003 不会报错，只会解出乱码——
    这条断言就是钉住那个数字的。"""
    on(monkeypatch, "linux")
    monkeypatch.setattr(browser, "_keyring_secret", lambda application: b"keyring-secret")
    got = browser._chromium_key(family(Path("/nonexistent")))
    assert got == hashlib.pbkdf2_hmac("sha1", b"keyring-secret", b"saltysalt", 1, 16)
    assert got != hashlib.pbkdf2_hmac("sha1", b"keyring-secret", b"saltysalt", 1003, 16)


def test_linux_falls_back_to_the_hardcoded_password(monkeypatch) -> None:
    """没装 gnome-keyring（或没登录图形会话）时，Chromium 自己就用 `peanuts`。"""
    on(monkeypatch, "linux")
    monkeypatch.setattr(browser, "_keyring_secret", lambda application: None)
    assert browser._chromium_key(family(Path("/nonexistent"))) == hashlib.pbkdf2_hmac(
        "sha1", b"peanuts", b"saltysalt", 1, 16
    )


def test_linux_prefers_the_keyring_over_the_fallback(monkeypatch) -> None:
    on(monkeypatch, "linux")
    monkeypatch.setattr(browser, "_keyring_secret", lambda application: b"real-secret")
    assert browser._chromium_key(family(Path("/nonexistent"))) != hashlib.pbkdf2_hmac(
        "sha1", b"peanuts", b"saltysalt", 1, 16
    )


def test_linux_keyring_lookup_uses_the_vendor_application_name(monkeypatch) -> None:
    """gnome-keyring 靠 application 属性区分浏览器，名字写错就永远查不到。"""
    seen: list[str] = []
    on(monkeypatch, "linux")
    monkeypatch.setattr(browser, "_keyring_secret", lambda application: seen.append(application) or b"x")
    browser._chromium_key(family(Path("/nonexistent"), key="brave", label="Brave"))
    assert seen == ["brave"]


def write_local_state(root: Path, key: bytes, *, prefix: bytes = b"DPAPI") -> None:
    import base64
    import json

    root.mkdir(parents=True, exist_ok=True)
    payload = {"os_crypt": {"encrypted_key": base64.b64encode(prefix + key).decode()}}
    (root / "Local State").write_text(json.dumps(payload), encoding="utf-8")


WINDOWS_KEY = bytes(range(32))


def test_windows_key_is_unwrapped_through_dpapi(tmp_path: Path, monkeypatch) -> None:
    on(monkeypatch, "win32")
    write_local_state(tmp_path, WINDOWS_KEY)
    unwrapped: list[bytes] = []

    def fake(blob: bytes) -> bytes:
        unwrapped.append(blob)
        return WINDOWS_KEY

    monkeypatch.setattr(browser, "_dpapi_unprotect", fake)
    assert browser._windows_key(tmp_path) == WINDOWS_KEY
    # DPAPI 那个前缀必须在交给系统之前剥掉，否则 CryptUnprotectData 会直接失败
    assert unwrapped == [WINDOWS_KEY]


def test_windows_key_rejects_a_local_state_without_the_dpapi_prefix(tmp_path: Path, monkeypatch) -> None:
    on(monkeypatch, "win32")
    write_local_state(tmp_path, WINDOWS_KEY, prefix=b"")
    with pytest.raises(BiliError) as info:
        browser._windows_key(tmp_path)
    assert "DPAPI" in info.value.message
    assert "--cookie-file" in (info.value.hint or "")


def test_windows_key_reports_a_missing_local_state(tmp_path: Path, monkeypatch) -> None:
    on(monkeypatch, "win32")
    with pytest.raises(BiliError) as info:
        browser._windows_key(tmp_path)
    assert "Local State" in info.value.message


# --------------------------------------------------------------------------- #
# Windows 的 AES-256-GCM
# --------------------------------------------------------------------------- #

GCM_KEY = bytes(range(32))
GCM_NONCE = b"\x01" * 12


def encrypt_gcm(
    plaintext: str, *, key: bytes = GCM_KEY, host: str = BOUND_HOST, bind: bool = True
) -> bytes:
    """造一份与 Windows Chromium 等价的密文。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = plaintext.encode()
    if bind:
        raw = hashlib.sha256(host.encode()).digest() + raw
    encryptor = Cipher(algorithms.AES(key), modes.GCM(GCM_NONCE)).encryptor()
    body = encryptor.update(raw) + encryptor.finalize()
    return b"v10" + GCM_NONCE + body + encryptor.tag


def test_gcm_round_trips_a_bound_value() -> None:
    assert browser.decrypt_value_gcm(encrypt_gcm("SESSDATA-VALUE"), GCM_KEY, BOUND_HOST) == "SESSDATA-VALUE"


def test_gcm_round_trips_an_unbound_value() -> None:
    blob = encrypt_gcm("plain", bind=False)
    assert browser.decrypt_value_gcm(blob, GCM_KEY, BOUND_HOST) == "plain"


def test_gcm_rejects_a_tampered_tag() -> None:
    """解不开就返回 None——一条坏 Cookie 不该让整次提取失败。"""
    blob = encrypt_gcm("secret")
    assert browser.decrypt_value_gcm(blob[:-1] + bytes([blob[-1] ^ 0xFF]), GCM_KEY, BOUND_HOST) is None


def test_gcm_rejects_the_wrong_key() -> None:
    blob = encrypt_gcm("secret")
    assert browser.decrypt_value_gcm(blob, bytes(32), BOUND_HOST) is None


def test_gcm_skips_a_blob_that_is_too_short_to_hold_a_nonce_and_tag() -> None:
    assert browser.decrypt_value_gcm(b"v10" + b"\x00" * 20, GCM_KEY, BOUND_HOST) is None


def test_gcm_refuses_anything_but_the_v10_prefix() -> None:
    """`v11` 是 macOS/Linux 的写法，Windows 上不该按 GCM 去解它。"""
    assert browser.decrypt_value_gcm(b"v11" + encrypt_gcm("x", bind=False)[3:], GCM_KEY, BOUND_HOST) is None


# --------------------------------------------------------------------------- #
# App-Bound 加密（Windows Chrome 127+）
# --------------------------------------------------------------------------- #


def test_read_chromium_reports_app_bound_instead_of_silence(tmp_path: Path, monkeypatch) -> None:
    """全是 v20 时不能说「库里没有 B站的 Cookie」——它明明有，只是解不开。"""
    on(monkeypatch, "win32")
    monkeypatch.setattr(browser, "_windows_key", lambda root: WINDOWS_KEY)
    make_chromium(tmp_path, [(".bilibili.com", "SESSDATA", b"v20" + b"\x00" * 40)])
    with pytest.raises(browser._AppBound):
        browser._read_chromium(family(tmp_path))


def test_read_chromium_keeps_the_cookies_it_can_still_read(tmp_path: Path, monkeypatch) -> None:
    """混着 v10 与 v20 时，能解的那些必须照常返回，不能因为有 v20 就整体放弃。"""
    on(monkeypatch, "win32")
    monkeypatch.setattr(browser, "_windows_key", lambda root: GCM_KEY)
    make_chromium(
        tmp_path,
        [
            (".bilibili.com", "SESSDATA", encrypt_gcm("readable")),
            (".bilibili.com", "bili_jct", b"v20" + b"\x00" * 40),
        ],
    )
    found = browser._read_chromium(family(tmp_path))
    assert found is not None
    assert found.mapping == {"SESSDATA": "readable"}


def test_extract_explains_app_bound_rather_than_asking_the_user_to_log_in(
    tmp_path: Path, monkeypatch
) -> None:
    on(monkeypatch, "win32")
    monkeypatch.setattr(browser, "_windows_key", lambda root: WINDOWS_KEY)
    make_chromium(tmp_path / "chrome", [(".bilibili.com", "SESSDATA", b"v20" + b"\x00" * 40)])
    monkeypatch.setattr(browser, "_chromium_families", lambda: [family(tmp_path / "chrome")])
    monkeypatch.setattr(browser, "_firefox_profiles", list)
    with pytest.raises(UsageError) as info:
        browser.extract("chrome")
    assert "App-Bound" in (info.value.hint or "")
    # 关键的区分：不能让人去重新登录，那是白费功夫
    assert "解不开" in (info.value.hint or "")


# --------------------------------------------------------------------------- #
# 各平台的路径
# --------------------------------------------------------------------------- #


def test_chromium_paths_differ_per_platform(tmp_path: Path, monkeypatch) -> None:
    """Windows 多一层 `User Data`，Linux 是扁平的 `~/.config` 名字——
    照着 macOS 的样子猜，只会得到「找不到 profile」。"""
    on(monkeypatch, "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert next(f for f in browser._chromium_families() if f.key == "chrome").root == (
        tmp_path / "Google" / "Chrome" / "User Data"
    )

    on(monkeypatch, "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert next(f for f in browser._chromium_families() if f.key == "chrome").root == tmp_path / "google-chrome"
    assert next(f for f in browser._chromium_families() if f.key == "edge").root == tmp_path / "microsoft-edge"

    on(monkeypatch, "darwin")
    assert next(f for f in browser._chromium_families() if f.key == "chrome").root == (
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    )


def test_firefox_roots_cover_the_platform_layouts(tmp_path: Path, monkeypatch) -> None:
    on(monkeypatch, "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert browser._firefox_roots() == [tmp_path / "Mozilla" / "Firefox" / "Profiles"]

    on(monkeypatch, "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # 原生安装直接放在 firefox/ 下，部分发行版与 Flatpak 会再包一层 Profiles/
    assert browser._firefox_roots() == [
        tmp_path / "mozilla" / "firefox",
        tmp_path / "mozilla" / "firefox" / "Profiles",
    ]


def test_firefox_profiles_finds_both_linux_layouts(tmp_path: Path, monkeypatch) -> None:
    on(monkeypatch, "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    make_firefox(tmp_path / "mozilla" / "firefox" / "plain.default", [])
    make_firefox(tmp_path / "mozilla" / "firefox" / "Profiles" / "snap.default", [])
    names = [name for name, _ in browser._firefox_profiles()]
    assert names == ["plain.default", "snap.default"]


def test_safari_hint_does_not_talk_about_macos_settings_elsewhere(monkeypatch) -> None:
    on(monkeypatch, "linux")
    assert "完全磁盘访问权限" not in browser._safari_hint()
    on(monkeypatch, "darwin")
    assert "完全磁盘访问权限" in browser._safari_hint()
