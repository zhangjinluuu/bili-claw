"""清晰度档位解析与流选择——FR-03 的核心实现。

**设计前提（需求文档 6.2 的警告）**：B站会对请求的 ``qn`` 做静默降级——
请求 4K 可能返回 1080P，而业务码依然是 0。更关键的是，``playurl`` **只返回被授予的
那一档流**，而不是该视频的全部档位（``accept_quality`` 才是完整目录，但它不区分
"存在" 与 "这个账号能拿"）。

所以正确做法只有一条：**把用户意图翻译成一个具体 qn 发出去，再拿实际返回的流
回头核对**。翻译用下面的 ``LADDER``，核对用 ``select_video``；任何没对上的情况
都必须把 ``degraded=True`` 连同原因交给调用方，由 ``--on-unavailable`` 决定
是提示后继续还是直接失败。绝不静默假装成功。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from .errors import AuthError, QualityUnavailableError, UsageError
from .models import DashAudio, DashVideo, PlayUrlData

# --------------------------------------------------------------------------- #
# 静态档位表：仅用于解析输入、翻译 qn 与展示
# --------------------------------------------------------------------------- #

#: qn → 展示名。档位号与像素高度不是一回事（100 是"智能修复"，112 是"1080P+"）。
QUALITY_NAMES: dict[int, str] = {
    6: "240P",
    16: "360P",
    32: "480P",
    64: "720P",
    74: "720P60",
    80: "1080P",
    100: "智能修复",
    112: "1080P+",
    116: "1080P60",
    120: "4K",
    125: "HDR",
    126: "杜比视界",
    127: "8K",
}

#: qn → 该档位的像素高度。
QN_HEIGHTS: dict[int, int] = {
    6: 240,
    16: 360,
    32: 480,
    64: 720,
    74: 720,
    80: 1080,
    100: 1080,
    112: 1080,
    116: 1080,
    120: 2160,
    125: 2160,
    126: 2160,
    127: 4320,
}

#: 未登录账号能拿到的最高档位。**实测结论**（2026-10，对 5 个不同视频探测）：
#: 无论视频本身提供到 4K 还是 8K，匿名请求的 ``granted`` 恒为 ``[32, 16]``。
#: 也就是说 480P 之上的一切档位都需要登录，与"视频提供哪些档位"无关。
ANONYMOUS_MAX_QN = 32

#: 需要大会员的档位（1080P+ 及以上）。
VIP_QUALITIES: frozenset[int] = frozenset({112, 116, 120, 125, 126, 127})

#: 登录即可解锁、但不需要大会员的档位（720P ~ 1080P）。
LOGIN_QUALITIES: frozenset[int] = frozenset({64, 74, 80, 100})

#: 当前B站策略下，某一档位需要什么权限。
PermissionTier = Literal["anonymous", "login", "vip"]

#: 用户可以直接写的像素高度。
HEIGHT_PRESETS: tuple[int, ...] = (240, 360, 480, 720, 1080, 1440, 2160, 4320)

#: 输入别名 → 像素高度（写 "4k" 比写 "2160" 自然）。
HEIGHT_ALIASES: dict[str, int] = {"2k": 1440, "4k": 2160, "8k": 4320}

#: "普通"档位阶梯，按高度升序。
#: 刻意不含 100（智能修复，属于画质修复而非原始分辨率）、125（HDR）、126（杜比视界）
#: ——这三种是替代性渲染，默认替用户选上会很意外；想用请直接写档位号。
LADDER: tuple[int, ...] = (6, 16, 32, 64, 74, 80, 112, 116, 120, 127)

#: 请求"最高可用"时发出去的 qn——发最高档，让B站按账号权限自行决定给到哪一档。
MAX_QN = 127

#: codecid → 编码名。
CODEC_NAMES: dict[int, str] = {7: "avc", 12: "hevc", 13: "av1"}

#: 默认编码偏好：兼容性优先（AVC 在老旧设备与剪辑软件里最稳）。
DEFAULT_CODEC_PREFERENCE: tuple[str, ...] = ("avc", "hevc", "av1")

#: ``--codec-compat`` 等价于默认偏好，显式声明用。
COMPAT_CODEC_PREFERENCE: tuple[str, ...] = ("avc", "hevc", "av1")

#: 音频 qn → 展示名。
AUDIO_QUALITY_NAMES: dict[int, str] = {
    30216: "64K",
    30232: "132K",
    30280: "192K",
    30250: "杜比全景声",
    30251: "Hi-Res 无损",
}

#: 标准 AAC 音轨，按码率从高到低。默认只在这三档里挑，保证合并出的 MP4 到处都能播。
STANDARD_AUDIO: tuple[int, ...] = (30280, 30232, 30216)

#: 高规格音轨：需要显式 ``--audio-quality best`` 才会选用。
PREMIUM_AUDIO: tuple[int, ...] = (30251, 30250)

_AUDIO_RANK: dict[int, int] = {30216: 1, 30232: 2, 30280: 3, 30250: 4, 30251: 5}


def nominal_height(qn: int) -> int:
    return QN_HEIGHTS.get(qn, 0)


# --------------------------------------------------------------------------- #
# 内部流表示：把线格式（camelCase、可能缺字段）转成干净的领域对象
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VideoStream:
    qn: int
    codecid: int
    codec: str
    width: int
    height: int
    fps: float
    bandwidth: int
    urls: tuple[str, ...]
    size: int | None = None

    @property
    def label(self) -> str:
        return QUALITY_NAMES.get(self.qn, f"qn{self.qn}")

    def describe(self) -> str:
        parts = [self.label, f"{self.width}×{self.height}"]
        if self.fps:
            parts.append(f"{self.fps:g}fps")
        parts.append(self.codec)
        parts.append(f"{self.bandwidth / 1000:.0f} kbps")
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class AudioStream:
    qn: int
    codecid: int
    bandwidth: int
    codecs: str
    urls: tuple[str, ...]
    size: int | None = None

    @property
    def label(self) -> str:
        return AUDIO_QUALITY_NAMES.get(self.qn, f"qn{self.qn}")

    @property
    def is_premium(self) -> bool:
        return self.qn in PREMIUM_AUDIO

    @property
    def is_lossless(self) -> bool:
        return self.codecs.startswith("flac") or self.qn == 30251

    def describe(self) -> str:
        return f"{self.label}（{self.bandwidth / 1000:.0f} kbps）"


def parse_frame_rate(value: object) -> float:
    """``frameRate`` 可能是 ``"30"``、``30``，也可能是 ``"16000/672"``。"""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            den = float(denominator)
            return float(numerator) / den if den else 0.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _urls(primary: str, backups: Iterable[str] | None) -> tuple[str, ...]:
    seen: list[str] = []
    for url in (primary, *(backups or ())):
        if url and url not in seen:
            seen.append(url)
    return tuple(seen)


def _video_stream(item: DashVideo) -> VideoStream:
    return VideoStream(
        qn=item.id,
        codecid=item.codecid,
        codec=CODEC_NAMES.get(item.codecid, "unknown"),
        width=item.width,
        height=item.height,
        fps=parse_frame_rate(item.frameRate),
        bandwidth=item.bandwidth,
        urls=_urls(item.baseUrl, item.backupUrl),
        size=item.size,
    )


def _audio_stream(item: DashAudio) -> AudioStream:
    return AudioStream(
        qn=item.id,
        codecid=item.codecid,
        bandwidth=item.bandwidth,
        codecs=item.codecs,
        urls=_urls(item.baseUrl, item.backupUrl),
        size=item.size,
    )


def to_video_streams(data: PlayUrlData) -> list[VideoStream]:
    """取出响应里的全部视频轨并转成领域对象。"""
    if not data.dash:
        return []
    return [_video_stream(item) for item in data.dash.video]


def to_audio_streams(data: PlayUrlData) -> list[AudioStream]:
    if not data.dash:
        return []
    return [_audio_stream(item) for item in data.dash.all_audio()]


# --------------------------------------------------------------------------- #
# 用户输入解析
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QualityRequest:
    """用户对"要什么清晰度"的表达。"""

    raw: str
    kind: Literal["max", "height", "qn", "ask"]
    target: int | None = None

    def describe(self) -> str:
        if self.kind == "max":
            return "最高可用"
        if self.kind == "ask":
            return "交互选择"
        if self.kind == "height":
            return f"不超过 {self.target}P"
        return QUALITY_NAMES.get(self.target or 0, f"qn{self.target}")


def parse_quality(raw: str) -> QualityRequest:
    """把 ``-q`` 的取值解析成 ``QualityRequest``。

    接受：``max``/``best``、``ask``（交互选择）、像素高度（``1080``、``1080p``、
    ``4k``）、以及 B站档位号（``80``、``116``）。
    """
    text = str(raw).strip().lower()
    if not text:
        raise UsageError("清晰度不能为空", hint=_quality_help())

    if text in {"max", "best", "highest"}:
        return QualityRequest(raw=raw, kind="max")

    # ask 与 max 一样不指定具体档位，要到拿到响应之后才能决定——区别在于由谁来选。
    if text == "ask":
        return QualityRequest(raw=raw, kind="ask")

    if text.endswith("p") and text[:-1].isdigit():
        text = text[:-1]

    if text in HEIGHT_ALIASES:
        return QualityRequest(raw=raw, kind="height", target=HEIGHT_ALIASES[text])

    if text.isdigit():
        number = int(text)
        # 高度表与档位号表没有交集（高度是 240/360/…，档位号是 6/16/80/…），
        # 所以先按高度解释不会产生歧义。
        if number in HEIGHT_PRESETS:
            return QualityRequest(raw=raw, kind="height", target=number)
        if number in QUALITY_NAMES:
            return QualityRequest(raw=raw, kind="qn", target=number)

    raise UsageError(f"无法识别的清晰度：{raw}", hint=_quality_help())


def _quality_help() -> str:
    heights = "、".join(str(h) for h in HEIGHT_PRESETS)
    names = "、".join(f"{qn}={name}" for qn, name in sorted(QUALITY_NAMES.items()))
    return (
        f"可用写法：max（最高可用）、ask（列出现有档位后再选）、"
        f"像素高度（{heights}）、或 B站档位号（{names}）"
    )


def sort_for_menu(
    streams: Iterable[VideoStream], preference: tuple[str, ...] = DEFAULT_CODEC_PREFERENCE
) -> list[VideoStream]:
    """把可选流排成「最适合从上往下挑」的顺序，供 ``-q ask`` 的菜单使用。

    先按像素高度降序，再按用户的编码偏好，最后按码率降序。这样第一项就是"最好的
    那个"，用户直接回车即可；同高度下的 AVC/HEVC/AV1 按用户偏好排列，而不是按
    接口返回的偶然顺序。
    """
    def rank(stream: VideoStream) -> tuple[int, int, int]:
        try:
            codec_rank = preference.index(stream.codec)
        except ValueError:
            codec_rank = len(preference)
        return (-stream.height, codec_rank, -stream.bandwidth)

    return sorted(streams, key=rank)


def parse_codec_preference(spec: str | None, *, compat: bool = False) -> tuple[str, ...]:
    """解析 ``--codec avc,hevc``；未列出的编码按默认顺序补在后面。"""
    if compat or not spec:
        return COMPAT_CODEC_PREFERENCE

    requested = [part.strip().lower() for part in spec.split(",") if part.strip()]
    if not requested:
        return COMPAT_CODEC_PREFERENCE

    known = set(CODEC_NAMES.values())
    invalid = [name for name in requested if name not in known]
    if invalid:
        raise UsageError(
            f"无法识别的编码：{', '.join(invalid)}",
            hint=f"可用编码：{', '.join(sorted(known))}",
        )

    tail = [name for name in DEFAULT_CODEC_PREFERENCE if name not in requested]
    return tuple(requested + tail)


def qn_for_request(request: QualityRequest) -> int:
    """把用户意图翻译成要发给 playurl 的 qn。

    * ``max`` / ``ask`` → 直接要最高档（``MAX_QN``），由B站按账号权限给到它能给的
      最高档。``ask`` 也必须先要最高档：playurl 只授予"请求档位降级后"的那一组流，
      请求 max 才能一次拿到该账号权限内的全部可选流，作为菜单数据。
    * ``height H`` → 取阶梯里**高度不超过 H 的最高档**，例如 ``-q 1440`` 会落到
      1080P60（B站没有 1440P 这一档）；
    * ``qn Q`` → 原样发出。
    """
    if request.kind in {"max", "ask"}:
        return MAX_QN
    if request.kind == "qn":
        assert request.target is not None
        return request.target

    assert request.target is not None
    candidates = [qn for qn in LADDER if nominal_height(qn) <= request.target]
    return candidates[-1] if candidates else LADDER[0]


# --------------------------------------------------------------------------- #
# 选择与降级判定
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Selection:
    """一次选择的结果，以及"是否发生了降级"的完整交代。"""

    stream: VideoStream
    requested: str
    degraded: bool
    notice: str | None = None


def _preference_index(preference: tuple[str, ...]):
    order = {name: index for index, name in enumerate(preference)}

    def index_of(stream: VideoStream) -> int:
        return order.get(stream.codec, len(order))

    return index_of


def _sort_key(preference: tuple[str, ...]):
    """排序键：高度 > 帧率 > 编码偏好 > 码率。取 ``max`` 即得最优。"""
    preference_index = _preference_index(preference)

    def key(stream: VideoStream) -> tuple[int, float, int, int]:
        return (
            stream.height,
            stream.fps,
            -preference_index(stream),  # 越靠前的偏好 → 负值越接近 0 → 越大 → 越优先
            stream.bandwidth,
        )

    return key


def describe_available(streams: Sequence[VideoStream], preference: tuple[str, ...]) -> list[str]:
    """把一组流按从高到低排好并转成可读文本。"""
    ordered = sorted(set(streams), key=_sort_key(preference), reverse=True)
    return [stream.describe() for stream in ordered]


def format_available(streams: Sequence[VideoStream], preference: tuple[str, ...]) -> str:
    return "\n".join(f"  - {line}" for line in describe_available(streams, preference))


def required_tier(qn: int) -> PermissionTier:
    """某一档位在当前B站策略下需要什么权限。"""
    if qn in VIP_QUALITIES:
        return "vip"
    if qn in LOGIN_QUALITIES:
        return "login"
    return "anonymous"


def _permission_hint(requested_qn: int, granted_qn: int, *, logged_in: bool | None = None) -> str:
    """降级是不是因为权限？这是用户最需要知道的一件事。

    ``logged_in`` 已知时必须利用它——"你没登录"和"你登录了但没大会员"是两种完全
    不同的处境，给出的下一步动作也不一样。
    """
    if granted_qn >= requested_qn:
        return ""

    name = QUALITY_NAMES.get(requested_qn, f"qn{requested_qn}")

    # 未登录时"登录"是第一道门槛，必须先说这个：对一个没登录的人强调"需要大会员"
    # 会把人带偏——他去开了大会员，依然拿不到，因为根本没登录。
    if logged_in is False and requested_qn > ANONYMOUS_MAX_QN:
        if requested_qn in VIP_QUALITIES:
            return f"；当前未登录（未登录上限 480P），且 {name} 还需要大会员——先提供登录态（--cookie-file）"
        return f"；未登录时最高只能下载 480P，{name} 需要登录（--cookie-file）"

    tier = required_tier(requested_qn)
    if tier == "vip":
        return f"；{name} 需要大会员"
    if tier == "login":
        return f"；{name} 需要登录（--cookie-file）"
    return ""


def _tier_denied_reason(
    requested_qn: int,
    got_qn: int,
    *,
    logged_in: bool | None,
    accept_quality: Sequence[int] | None,
) -> str:
    """拿到了**同分辨率但更低档位**时的说明（如请求 1080P60、实际给 1080P）。

    这里问的是「片源有没有我们请求的那个 qn」——此时 got_qn 已经落在同一高度上，
    只有片源确实挂过 requested_qn 却没给我们，才谈得上权限。
    """
    if accept_quality is not None and requested_qn not in set(accept_quality):
        return ""
    return _permission_hint(requested_qn, got_qn, logged_in=logged_in)


def _degraded_reason(
    request: QualityRequest,
    granted_qn: int,
    got_qn: int,
    *,
    logged_in: bool | None,
    accept_quality: Sequence[int] | None,
) -> str:
    """真正降级时的一句「为什么＋下一步」。

    判据必须用 `_is_permission_wall`（问"用户能接受的档位里还有没有没给我们的"），
    **不能**用 `_tier_denied_reason` 那种"请求的 qn 在不在目录里"——`-q 720` 会请求
    qn=74（720P60），而片源常把 720P 挂在 qn=64 上。用后者判会把"该登录"误报成
    "档位不存在"，正好丢掉 E2E-10 最该给的那句提示。
    """
    if not _is_permission_wall(
        request, granted_qn, nominal_height(got_qn), accept_quality=accept_quality
    ):
        return ""
    return _permission_hint(granted_qn, got_qn, logged_in=logged_in)


def _is_permission_wall(
    request: QualityRequest,
    granted_qn: int,
    got_height: int,
    *,
    accept_quality: Sequence[int] | None,
) -> bool:
    """能不能**确凿地**说这是权限问题？

    判据不是"我们请求的那个 qn 在不在目录里"——`-q 720` 会请求 qn=74（720P60），
    而片源可能只把 720P 挂在 qn=64 上。用户要的是"720P"，不是"74"。

    所以真正该问的是：**片源里还有没有「用户能接受、却偏偏没给我们」的更好档位？**
    有 → 卡在权限上；没有 → 这视频本来就只有这么高，登录也没用。

    目录未知时不敢断言，保守按"档位不对"处理——把"本来没有 1080P"报成"需要登录"，
    会让用户白折腾一趟登录甚至开会员。
    """
    if accept_quality is None:
        return False
    if request.kind == "qn":
        return granted_qn in set(accept_quality)

    ceiling = nominal_height(granted_qn)
    return any(
        nominal_height(qn) > got_height and (not ceiling or nominal_height(qn) <= ceiling)
        for qn in accept_quality
    )


def select_video(
    streams: Sequence[VideoStream],
    request: QualityRequest,
    *,
    granted_qn: int,
    preference: tuple[str, ...] = DEFAULT_CODEC_PREFERENCE,
    on_unavailable: Literal["degrade", "fail"] = "degrade",
    accept_quality: Sequence[int] | None = None,
    logged_in: bool | None = None,
) -> Selection:
    """在接口**实际返回**的流里挑一条，并判定是否发生了降级。

    ``granted_qn`` 是我们请求的 qn（``qn_for_request`` 的结果）。B站返回的同档位
    流可能有多条（不同编码各一条），这里按编码偏好与码率挑最好的那条。
    """
    if not streams:
        raise QualityUnavailableError(
            "该视频没有可用的 DASH 视频流",
            hint="可能是付费/受限内容，或接口返回了非 DASH 格式（老视频见 R8）",
        )

    key = _sort_key(preference)
    ceiling = nominal_height(granted_qn)

    # 正常情况下返回的流高度就等于请求档位的高度；若B站给了更高的（理论上不该发生），
    # 按"不超过"的语义把它排除掉，避免 `-q 720` 拿到 1080P。
    within_ceiling = [stream for stream in streams if not ceiling or stream.height <= ceiling]
    candidates = within_ceiling or list(streams)
    best = max(candidates, key=key)

    if request.kind == "max":
        # 没有明确请求档位，就不存在"降级"；但仍要如实告知拿到了什么、为什么没更高。
        notice = None
        if accept_quality and max(accept_quality) > best.qn:
            higher = [qn for qn in accept_quality if qn > best.qn and qn in QUALITY_NAMES]
            if higher:
                names = "、".join(QUALITY_NAMES[qn] for qn in sorted(set(higher)))
                reason = _permission_hint(max(higher), best.qn, logged_in=logged_in)
                notice = f"该视频还提供 {names}{reason}"
        return Selection(stream=best, requested=request.describe(), degraded=False, notice=notice)

    # 明确请求了档位：按"用户真正在意的维度"判定降级
    if request.kind == "height":
        assert request.target is not None
        degraded = best.height < request.target
        detail = f"请求的 {request.target}P 不可用，已降级为 {best.label}"
    else:
        degraded = best.qn != granted_qn
        asked = QUALITY_NAMES.get(granted_qn, f"qn{granted_qn}")
        detail = f"请求的 {asked} 不可用，已降级为 {best.label}"

    if not degraded:
        # 拿到了想要的清晰度。若请求的是大会员档位而实际给了同分辨率的普通档，
        # 值得说明一句，但不构成降级。
        if best.qn < granted_qn:
            hint = _tier_denied_reason(
                granted_qn, best.qn, logged_in=logged_in, accept_quality=accept_quality
            )
            if hint:
                asked = QUALITY_NAMES.get(granted_qn, f"qn{granted_qn}")
                return Selection(
                    stream=best,
                    requested=request.describe(),
                    degraded=False,
                    notice=f"已获取 {best.label}；{asked}{hint.lstrip('；')}",
                )
        return Selection(stream=best, requested=request.describe(), degraded=False)

    if on_unavailable == "fail":
        # 严格模式的说明放进 hint：它是"下一步怎么办"，塞进 message 会把
        # "为什么拿不到"这句话从中间劈开，读起来像两件事。
        reason = _degraded_reason(
            request, granted_qn, best.qn, logged_in=logged_in, accept_quality=accept_quality
        )
        relax = "去掉 --on-unavailable=fail 即可自动降级到可用档位"
        if reason:
            # 真正的原因是权限而不是参数写错：退出码要让脚本能分辨（FR-11 的 2 与 4）。
            raise AuthError(f"{detail}{reason}", hint=f"提供登录态后重试，或{relax}")
        raise QualityUnavailableError(
            f"{detail}，且当前是严格模式",
            hint=f"{relax}；或改用该视频实际提供的档位",
        )

    return Selection(
        stream=best,
        requested=request.describe(),
        degraded=True,
        notice=detail
        + _degraded_reason(
            request, granted_qn, best.qn, logged_in=logged_in, accept_quality=accept_quality
        ),
    )


def select_audio(streams: Sequence[AudioStream], *, want: str = "auto") -> AudioStream | None:
    """选择音轨。

    ``auto``（默认）只挑标准 AAC 音轨里码率最高的一条——杜比全景声与 Hi-Res 无损
    虽然规格更高，但合并进 MP4 后在部分播放器上无法解码，不该在用户没要求时默认选中。
    想要它们请显式写 ``--audio-quality best`` 或档位号。
    """
    if not streams:
        return None

    text = str(want).strip().lower()

    if text in {"best", "max"}:
        return max(streams, key=lambda s: (_AUDIO_RANK.get(s.qn, 0), s.bandwidth))

    if text in {"auto", "standard", ""}:
        standard = [s for s in streams if s.qn in STANDARD_AUDIO]
        pool = list(standard or streams)
        return max(pool, key=lambda s: (_AUDIO_RANK.get(s.qn, 0), s.bandwidth))

    if text.isdigit():
        qn = int(text)
        exact = [s for s in streams if s.qn == qn]
        if exact:
            return max(exact, key=lambda s: s.bandwidth)
        raise UsageError(
            f"该视频没有 {AUDIO_QUALITY_NAMES.get(qn, f'qn{qn}')} 音轨",
            hint="可用音轨：" + "、".join(sorted({s.label for s in streams})),
        )

    raise UsageError(
        f"无法识别的音轨选项：{want}",
        hint="可用写法：auto（默认，最高标准音轨）、best（含杜比/Hi-Res）、或音轨档位号如 30280",
    )
