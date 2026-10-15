"""WBI 签名测试（需求文档 6.3）。

`mixin_key` 的期望值取自B站官方接口文档里的公开示例，可作为回归基准：
只要重排表或拼接顺序被改错，这里立刻会红。
"""

from __future__ import annotations

from biliclaw.wbi import MIXIN_KEY_ENC_TAB, key_from_url, mixin_key, sign

# 公开的示例 wbi 密钥（来自 nav 接口的 wbi_img）。
IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
MIXIN = "ea1db124af3c7062474693fa704f4ff8"


def test_enc_tab_is_a_valid_permutation() -> None:
    """重排表必须是 0..63 的一个排列，漏一个或重一个都会让签名静默错位。"""
    assert len(MIXIN_KEY_ENC_TAB) == 64
    assert sorted(MIXIN_KEY_ENC_TAB) == list(range(64))


def test_mixin_key_matches_the_reference_value() -> None:
    assert mixin_key(IMG_KEY, SUB_KEY) == MIXIN


def test_mixin_key_is_always_32_chars() -> None:
    assert len(mixin_key(IMG_KEY, SUB_KEY)) == 32
    assert len(mixin_key("", "")) == 0


def test_mixin_key_is_not_just_a_concatenation() -> None:
    """如果实现退化成拼接，签名会全错但看不出来——这里显式挡住。"""
    assert mixin_key(IMG_KEY, SUB_KEY) != (IMG_KEY + SUB_KEY)[:32]


def test_key_from_url() -> None:
    assert key_from_url("https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png") == IMG_KEY
    assert key_from_url("4932caff0ff746eab6f01bf08b70ac45.png") == SUB_KEY


def test_key_from_url_without_extension() -> None:
    assert key_from_url("https://example.com/abcdef") == "abcdef"


def test_sign_adds_wts_and_w_rid() -> None:
    params = sign(
        {"bvid": "BV1xx411c7mD", "cid": 62131, "qn": 80, "fnval": 4048, "fourk": 1},
        IMG_KEY,
        SUB_KEY,
        timestamp=1700000000,
    )
    assert params["wts"] == "1700000000"
    assert params["w_rid"] == "2a4d9278b72ad20914a3b289eb5900d2"


def test_sign_stringifies_values() -> None:
    params = sign({"cid": 62131, "qn": 80, "fourk": 1}, IMG_KEY, SUB_KEY, timestamp=1700000000)
    assert params["cid"] == "62131"
    assert params["qn"] == "80"
    assert params["fourk"] == "1"
    assert all(isinstance(value, str) for value in params.values())


def test_sign_is_order_independent() -> None:
    """签名内部按 key 排序，因此调用方传入顺序不影响结果。"""
    first = sign({"bvid": "BV1", "cid": 1, "qn": 80}, IMG_KEY, SUB_KEY, timestamp=1700000000)
    second = sign({"qn": 80, "cid": 1, "bvid": "BV1"}, IMG_KEY, SUB_KEY, timestamp=1700000000)
    assert first["w_rid"] == second["w_rid"]


def test_sign_strips_the_reserved_characters() -> None:
    """B站前端会把参数值里的 !'()* 去掉再签名，不照做会导致 w_rid 不匹配。"""
    with_junk = sign({"keyword": "a'b(c)d!e*f"}, IMG_KEY, SUB_KEY, timestamp=1700000000)
    cleaned = sign({"keyword": "abcdef"}, IMG_KEY, SUB_KEY, timestamp=1700000000)
    assert with_junk["w_rid"] == cleaned["w_rid"]
    assert with_junk["keyword"] == "abcdef"


def test_sign_does_not_mutate_the_input() -> None:
    original = {"bvid": "BV1"}
    sign(original, IMG_KEY, SUB_KEY, timestamp=1700000000)
    assert original == {"bvid": "BV1"}


def test_sign_changes_with_params() -> None:
    first = sign({"qn": 80}, IMG_KEY, SUB_KEY, timestamp=1700000000)
    second = sign({"qn": 116}, IMG_KEY, SUB_KEY, timestamp=1700000000)
    assert first["w_rid"] != second["w_rid"]


def test_sign_changes_with_timestamp() -> None:
    first = sign({"qn": 80}, IMG_KEY, SUB_KEY, timestamp=1700000000)
    second = sign({"qn": 80}, IMG_KEY, SUB_KEY, timestamp=1700000001)
    assert first["w_rid"] != second["w_rid"]


def test_sign_changes_with_keys() -> None:
    first = sign({"qn": 80}, IMG_KEY, SUB_KEY, timestamp=1700000000)
    second = sign({"qn": 80}, SUB_KEY, IMG_KEY, timestamp=1700000000)
    assert first["w_rid"] != second["w_rid"]
