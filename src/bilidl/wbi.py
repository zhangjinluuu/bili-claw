"""WBI 请求签名（需求文档 6.3）。

B站自 2023 年起对 ``/x/player/wbi/playurl`` 等接口要求 WBI 签名，缺少或错误的
``w_rid`` 会直接返回 -403。签名流程：

1. 请求 ``/x/web-interface/nav``，取 ``wbi_img.img_url`` / ``sub_url`` 的文件名主干
   作为 ``img_key`` / ``sub_key``；
2. 拼接后按固定的 64 位置换表重排，取前 32 位得到 ``mixin_key``（按天轮换）；
3. 请求参数先剔除 ``!'()*`` 等字符，按参数名排序拼成 query，追加 ``wts`` 时间戳；
4. ``w_rid = md5(query + mixin_key)``，与 ``wts`` 一起作为参数发出。
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import urlencode

#: mixin key 的固定重排表。
MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

#: 需要在签名前从参数值中剔除的字符（B站前端同款规则）。
_FILTERED_CHARS = "!'()*"


def key_from_url(url: str) -> str:
    """从 ``.../bfs/wbi/7cd084941338484aae1ad9425b84077c.png`` 取出 key。"""
    name = url.rsplit("/", 1)[-1]
    return name.split(".", 1)[0]


def mixin_key(img_key: str, sub_key: str) -> str:
    """按重排表生成 ``mixin_key``（取前 32 位）。"""
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]


def _clean(value: object) -> str:
    text = str(value)
    for char in _FILTERED_CHARS:
        text = text.replace(char, "")
    return text


def sign(
    params: dict[str, object],
    img_key: str,
    sub_key: str,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    """给 ``params`` 加上 ``wts`` 与 ``w_rid``，返回可直接用于请求的新字典。

    ``timestamp`` 可注入以便测试；默认取当前秒级时间戳。
    """
    key = mixin_key(img_key, sub_key)
    signed = {name: _clean(value) for name, value in params.items()}
    signed["wts"] = str(int(time.time()) if timestamp is None else timestamp)
    query = urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return signed
