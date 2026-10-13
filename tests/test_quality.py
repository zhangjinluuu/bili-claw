"""清晰度协商测试——FR-03 的核心，也是本项目区别于其它下载器的地方。

这里锁定的行为不是"能挑出一条流"，而是**降级必须被如实识别并解释清楚**。
"""

from __future__ import annotations

import pytest

from bilidl.errors import AuthError, ExitCode, QualityUnavailableError, UsageError
from bilidl.quality import (
    ANONYMOUS_MAX_QN,
    AudioStream,
    QualityRequest,
    VideoStream,
    parse_codec_preference,
    parse_quality,
    qn_for_request,
    required_tier,
    select_audio,
    select_video,
)

AVC, HEVC, AV1 = 7, 12, 13
PREF = ("avc", "hevc", "av1")

# 一个"该有的档位都有"的视频目录，贴近真实 playurl 返回的 accept_quality。
# 真实接口总会给这个字段，所以不传它（None）只该出现在"接口没给"的防御场景里。
FULL_CATALOG = [127, 126, 125, 120, 116, 112, 80, 74, 64, 32, 16]


def vs(
    qn: int,
    height: int,
    *,
    codec: str = "avc",
    codecid: int = AVC,
    fps: float = 30.0,
    bandwidth: int = 1_000_000,
) -> VideoStream:
    return VideoStream(
        qn=qn,
        codecid=codecid,
        codec=codec,
        width=int(height * 16 / 9),
        height=height,
        fps=fps,
        bandwidth=bandwidth,
        urls=(f"https://cdn.example/{qn}-{codec}.m4s",),
    )


def audio(qn: int, bandwidth: int, codecs: str = "mp4a.40.2") -> AudioStream:
    return AudioStream(qn=qn, codecid=30216, bandwidth=bandwidth, codecs=codecs, urls=("https://cdn.example/a.m4s",))


# --------------------------------------------------------------------------- #
# -q 的解析
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["max", "best", "highest", "MAX", " max "])
def test_parse_quality_max(raw: str) -> None:
    assert parse_quality(raw).kind == "max"


@pytest.mark.parametrize("raw,target", [("1080", 1080), ("1080p", 1080), ("720", 720), ("4k", 2160), ("8K", 4320)])
def test_parse_quality_height(raw: str, target: int) -> None:
    request = parse_quality(raw)
    assert (request.kind, request.target) == ("height", target)


@pytest.mark.parametrize("raw,qn", [("80", 80), ("116", 116), ("127", 127), ("6", 6)])
def test_parse_quality_qn_number(raw: str, qn: int) -> None:
    """档位号与像素高度没有交集，所以数字先按高度解释不会产生歧义。"""
    request = parse_quality(raw)
    assert (request.kind, request.target) == ("qn", qn)


@pytest.mark.parametrize("raw", ["", "  ", "超清", "10000p", "999"])
def test_parse_quality_rejects_garbage(raw: str) -> None:
    with pytest.raises(UsageError):
        parse_quality(raw)


# --------------------------------------------------------------------------- #
# 意图 → qn 的翻译
# --------------------------------------------------------------------------- #


def test_max_asks_for_the_top_tier() -> None:
    """max 直接要 127，由B站按账号权限决定给到哪一档。"""
    assert qn_for_request(parse_quality("max")) == 127


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("240", 6),
        ("480", 32),
        ("720", 74),  # 720P60 也 ≤720，按"不超过"语义取最高
        ("1080", 116),  # 1080P60 也 ≤1080
        ("1440", 116),  # B站没有 1440P 这一档，落到 1080P60
        ("2160", 120),
        ("4320", 127),
    ],
)
def test_height_maps_to_highest_tier_not_exceeding_it(raw: str, expected: int) -> None:
    assert qn_for_request(parse_quality(raw)) == expected


# --------------------------------------------------------------------------- #
# 权限分层
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "qn,tier",
    [(6, "anonymous"), (16, "anonymous"), (32, "anonymous"), (64, "login"), (74, "login"), (80, "login"),
     (100, "login"), (112, "vip"), (116, "vip"), (120, "vip"), (127, "vip")],
)
def test_required_tier(qn: int, tier: str) -> None:
    assert required_tier(qn) == tier


def test_anonymous_ceiling_is_480p() -> None:
    """实测结论：匿名请求 5 个视频一律只拿到 [32, 16]，与视频提供哪些档位无关。"""
    assert ANONYMOUS_MAX_QN == 32
    assert required_tier(ANONYMOUS_MAX_QN) == "anonymous"
    assert required_tier(64) == "login"


# --------------------------------------------------------------------------- #
# max：不构成降级，但要说明"为什么没有更高"
# --------------------------------------------------------------------------- #


def test_max_picks_the_tallest_stream() -> None:
    streams = [vs(16, 360), vs(32, 480), vs(80, 1080)]
    result = select_video(streams, parse_quality("max"), granted_qn=127)
    assert result.stream.qn == 80
    assert result.degraded is False


def test_max_explains_why_higher_tiers_are_missing() -> None:
    """没有明确请求档位就不算降级，但用户仍然有权知道上面还有什么、以及为什么拿不到。"""
    streams = [vs(32, 480)]
    result = select_video(
        streams,
        parse_quality("max"),
        granted_qn=127,
        accept_quality=[120, 112, 80, 64, 32, 16],
        logged_in=False,
    )
    assert result.degraded is False
    assert result.notice is not None
    assert "4K" in result.notice and "1080P" in result.notice
    assert "未登录" in result.notice


def test_max_without_higher_tiers_is_silent() -> None:
    streams = [vs(32, 480), vs(16, 360)]
    result = select_video(streams, parse_quality("max"), granted_qn=127, accept_quality=[32, 16])
    assert result.notice is None


# --------------------------------------------------------------------------- #
# 明确请求档位：降级必须被识别、解释、并可选择失败
# --------------------------------------------------------------------------- #


def test_degradation_is_detected_for_height_request() -> None:
    result = select_video([vs(32, 480)], parse_quality("1080"), granted_qn=116)
    assert result.degraded is True
    assert result.stream.qn == 32
    assert "480P" in (result.notice or "")


def test_anonymous_degradation_leads_with_the_login_problem() -> None:
    """对一个没登录的人强调"需要大会员"会把人带偏：他去开了会员依然拿不到。"""
    result = select_video(
        [vs(32, 480)],
        parse_quality("1080"),
        granted_qn=116,
        logged_in=False,
        accept_quality=FULL_CATALOG,
    )
    notice = result.notice or ""
    assert "未登录" in notice
    assert notice.index("未登录") < notice.index("大会员")
    assert "--cookie-file" in notice


def test_logged_in_degradation_points_at_vip() -> None:
    """已登录却只拿到 480P，说明卡在大会员这一层，不该再叫人去登录。"""
    result = select_video(
        [vs(32, 480)],
        parse_quality("1080"),
        granted_qn=116,
        logged_in=True,
        accept_quality=FULL_CATALOG,
    )
    notice = result.notice or ""
    assert "大会员" in notice
    assert "未登录" not in notice


def test_720p_degrades_to_login_hint_not_vip_hint() -> None:
    """`-q 720` 请求的是 qn=74，而片源把 720P 挂在 qn=64 上——提示不能因此丢掉。"""
    result = select_video(
        [vs(32, 480)],
        parse_quality("720"),
        granted_qn=74,
        logged_in=False,
        accept_quality=FULL_CATALOG,
    )
    notice = result.notice or ""
    assert "720P60" in notice
    assert "需要登录" in notice
    assert "大会员" not in notice


def test_degradation_stays_silent_about_permissions_when_no_higher_tier_exists() -> None:
    """片源本身就只到 480P：不能叫人去登录，登了也还是 480P。"""
    result = select_video(
        [vs(32, 480)],
        parse_quality("1080"),
        granted_qn=116,
        logged_in=False,
        accept_quality=[32, 16],
    )
    notice = result.notice or ""
    assert "480P" in notice
    assert "登录" not in notice
    assert "大会员" not in notice


def test_fail_mode_raises_instead_of_degrading() -> None:
    with pytest.raises(QualityUnavailableError):
        select_video([vs(32, 480)], parse_quality("1080"), granted_qn=116, on_unavailable="fail")


def test_fail_mode_reports_an_auth_wall_when_the_video_really_has_that_tier() -> None:
    """片源有 1080P60、只是没给我们 → 退出码要让脚本看出"该登录了"（FR-11 的 4）。"""
    with pytest.raises(AuthError) as excinfo:
        select_video(
            [vs(32, 480)],
            parse_quality("1080"),
            granted_qn=116,
            on_unavailable="fail",
            accept_quality=[116, 80, 64, 32, 16],
            logged_in=False,
        )
    assert excinfo.value.exit_code == ExitCode.AUTH
    assert "未登录" in excinfo.value.message


def test_fail_mode_stays_a_usage_error_when_the_video_lacks_that_tier() -> None:
    """视频根本没有 1080P → 这是档位选错了，不是权限问题，别把人引去开会员。"""
    with pytest.raises(QualityUnavailableError) as excinfo:
        select_video(
            [vs(32, 480)],
            parse_quality("1080"),
            granted_qn=116,
            on_unavailable="fail",
            accept_quality=[32, 16],
            logged_in=False,
        )
    assert excinfo.value.exit_code == ExitCode.USAGE


def test_fail_mode_sees_through_the_qn_mismatch() -> None:
    """回归：`-q 720` 请求 qn=74，片源把 720P 挂在 qn=64 上。

    曾经因为拿 74 去比对 accept_quality 而误判成"档位不存在"（退出码 2），
    实际上片源有 720P、只是没登录拿不到——必须报 4。
    """
    with pytest.raises(AuthError) as excinfo:
        select_video(
            [vs(32, 480)],
            parse_quality("720"),
            granted_qn=74,
            on_unavailable="fail",
            accept_quality=[64, 32, 16],
            logged_in=False,
        )
    assert excinfo.value.exit_code == ExitCode.AUTH
    assert "未登录" in excinfo.value.message


def test_degrade_notice_stays_quiet_about_permissions_when_the_tier_is_absent() -> None:
    """同上，降级提示里也不该出现"登录/大会员"。"""
    result = select_video(
        [vs(32, 480)],
        parse_quality("1080"),
        granted_qn=116,
        accept_quality=[32, 16],
        logged_in=False,
    )
    assert result.degraded is True
    notice = result.notice or ""
    assert "登录" not in notice
    assert "大会员" not in notice
    assert "480P" in notice


def test_exact_qn_request_that_is_satisfied_is_not_a_degradation() -> None:
    result = select_video([vs(80, 1080)], parse_quality("80"), granted_qn=80)
    assert result.stream.qn == 80
    assert result.degraded is False
    assert result.notice is None


def test_exact_qn_request_that_is_downgraded_is_a_degradation() -> None:
    result = select_video([vs(32, 480)], parse_quality("80"), granted_qn=80)
    assert result.degraded is True
    assert "1080P" in (result.notice or "")


# --------------------------------------------------------------------------- #
# 上限与排序
# --------------------------------------------------------------------------- #


def test_does_not_exceed_the_requested_ceiling() -> None:
    """`-q 720` 不该拿到 1080P——接口理论上不会这么返回，但真返回了必须挡住。"""
    result = select_video([vs(80, 1080), vs(74, 720)], parse_quality("720"), granted_qn=74)
    assert result.stream.height == 720


def test_higher_fps_wins_within_the_same_height() -> None:
    result = select_video([vs(80, 1080, fps=30), vs(116, 1080, fps=60)], parse_quality("max"), granted_qn=127)
    assert result.stream.fps == 60


def test_codec_preference_breaks_ties() -> None:
    streams = [
        vs(80, 1080, codec="av1", codecid=AV1, bandwidth=900_000),
        vs(80, 1080, codec="hevc", codecid=HEVC, bandwidth=1_100_000),
        vs(80, 1080, codec="avc", codecid=AVC, bandwidth=1_500_000),
    ]
    assert select_video(streams, parse_quality("max"), granted_qn=127).stream.codec == "avc"
    assert select_video(streams, parse_quality("max"), granted_qn=127, preference=("av1", "hevc", "avc")).stream.codec == "av1"


def test_bandwidth_breaks_codec_ties() -> None:
    streams = [vs(80, 1080, codec="avc", bandwidth=1_000_000), vs(80, 1080, codec="avc", bandwidth=2_000_000)]
    assert select_video(streams, parse_quality("max"), granted_qn=127).stream.bandwidth == 2_000_000


def test_no_dash_streams_is_an_error() -> None:
    with pytest.raises(QualityUnavailableError):
        select_video([], parse_quality("max"), granted_qn=127)


@pytest.mark.parametrize("spec,expected", [("avc,hevc", ("avc", "hevc", "av1")), ("av1", ("av1", "avc", "hevc")), (None, PREF)])
def test_parse_codec_preference(spec: str | None, expected: tuple[str, ...]) -> None:
    assert parse_codec_preference(spec) == expected


def test_parse_codec_preference_rejects_unknown() -> None:
    with pytest.raises(UsageError):
        parse_codec_preference("h264")


def test_codec_compat_always_yields_avc_first() -> None:
    assert parse_codec_preference("av1", compat=True)[0] == "avc"


# --------------------------------------------------------------------------- #
# 音轨
# --------------------------------------------------------------------------- #


def test_audio_auto_prefers_the_best_standard_track() -> None:
    """杜比/Hi-Res 规格更高，但合并进 MP4 后部分播放器放不出来，不该默认选中。"""
    streams = [audio(30216, 64_000), audio(30232, 132_000), audio(30280, 192_000), audio(30250, 448_000)]
    assert select_audio(streams).qn == 30280


def test_audio_best_opts_into_premium_tracks() -> None:
    streams = [audio(30280, 192_000), audio(30250, 448_000)]
    assert select_audio(streams, want="best").qn == 30250


def test_audio_explicit_qn() -> None:
    streams = [audio(30216, 64_000), audio(30280, 192_000)]
    assert select_audio(streams, want="30216").qn == 30216


def test_audio_missing_explicit_qn_is_a_usage_error() -> None:
    with pytest.raises(UsageError):
        select_audio([audio(30216, 64_000)], want="30280")


def test_audio_empty_returns_none() -> None:
    assert select_audio([]) is None


def test_audio_rejects_garbage_option() -> None:
    with pytest.raises(UsageError):
        select_audio([audio(30216, 64_000)], want="lossless")


def test_lossless_detection() -> None:
    assert audio(30251, 1_000_000, codecs="fLaC").is_lossless
    assert not audio(30280, 192_000).is_lossless


def test_quality_request_describe() -> None:
    assert parse_quality("max").describe() == "最高可用"
    assert parse_quality("1080").describe() == "不超过 1080P"
    assert parse_quality("80").describe() == "1080P"
