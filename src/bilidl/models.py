"""B站接口返回数据的结构化模型。

字段名刻意保持与接口返回的 camelCase 一致：这些模型是**线格式的镜像**，
不做重命名可以让「接口返回了什么」与「代码里读的是什么」一一对应，
B站改接口时也更容易定位差异。

所有模型都配置为 ``extra="ignore"``——B站会不定期加字段，
未知字段不应导致解析失败；但**已知字段的类型不符**会立刻抛错，
这正是我们想要的「接口变更早期信号」。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ApiEnvelope(_Model):
    """B站统一响应外壳 ``{code, message, ttl, data}``。"""

    code: int
    message: str = ""
    ttl: int = 1
    data: Any = None


class Page(_Model):
    """分P信息。"""

    cid: int = 0
    page: int = 1
    part: str = ""
    duration: int = 0


class Owner(_Model):
    mid: int = 0
    name: str = ""


class VideoInfo(_Model):
    """``/x/web-interface/view`` 的 data。"""

    bvid: str = ""
    aid: int = 0
    cid: int = 0
    title: str = ""
    desc: str = ""
    pic: str = ""
    pubdate: int = 0
    duration: int = 0
    owner: Owner = Field(default_factory=Owner)
    pages: list[Page] = Field(default_factory=list)

    def page_list(self) -> list[Page]:
        """返回分P列表；单P视频接口可能不给 ``pages``，用首页 cid 兜底。"""
        if self.pages:
            return self.pages
        return [Page(cid=self.cid, page=1, part="", duration=self.duration)]


class DashVideo(_Model):
    id: int = 0  # 即 qn
    baseUrl: str = ""
    backupUrl: list[str] = Field(default_factory=list)
    bandwidth: int = 0
    mimeType: str = ""
    codecs: str = ""
    width: int = 0
    height: int = 0
    frameRate: str | float | int = "0"  # 可能是 "30"、"60"，也可能是 "16000/672"
    codecid: int = 0
    size: int | None = None


class DashAudio(_Model):
    id: int = 0  # 即音频 qn，如 30280
    baseUrl: str = ""
    backupUrl: list[str] = Field(default_factory=list)
    bandwidth: int = 0
    mimeType: str = ""
    codecs: str = ""
    codecid: int = 0
    size: int | None = None


class DashDolby(_Model):
    type: int = 0
    audio: list[DashAudio] | None = None


class DashFlac(_Model):
    display: bool = False
    audio: DashAudio | None = None


class Dash(_Model):
    duration: int = 0
    video: list[DashVideo] = Field(default_factory=list)
    audio: list[DashAudio] | None = None
    dolby: DashDolby | None = None
    flac: DashFlac | None = None

    def all_audio(self) -> list[DashAudio]:
        """把普通音轨、杜比音轨、Hi-Res 无损音轨合并成一个列表。"""
        tracks: list[DashAudio] = list(self.audio or [])
        if self.dolby and self.dolby.audio:
            tracks.extend(self.dolby.audio)
        if self.flac and self.flac.audio:
            tracks.append(self.flac.audio)
        return tracks


class SupportFormat(_Model):
    """``accept_quality`` 对应的档位描述。"""

    quality: int = 0
    format: str = ""
    new_description: str = ""
    display_desc: str = ""
    superscript: str = ""
    codecs: list[str] | None = None


class Durl(_Model):
    """FLV/MP4 整段地址，仅在 DASH 不可用时出现。"""

    order: int = 0
    length: int = 0
    size: int = 0
    url: str = ""
    backup_url: list[str] | None = None


class PlayUrlData(_Model):
    """``/x/player/wbi/playurl`` 的 data。"""

    quality: int = 0
    format: str = ""
    timelength: int = 0
    accept_quality: list[int] = Field(default_factory=list)
    accept_description: list[str] = Field(default_factory=list)
    support_formats: list[SupportFormat] = Field(default_factory=list)
    dash: Dash | None = None
    durl: list[Durl] | None = None

    def audio_hint(self) -> str | None:
        """若存在杜比/Hi-Res 音轨，返回一句供用户参考的提示。"""
        if not self.dash:
            return None
        found = [t.id for t in self.dash.all_audio() if t.id in (30250, 30251)]
        if not found:
            return None
        names = {30250: "杜比全景声", 30251: "Hi-Res 无损"}
        return "、".join(names[q] for q in sorted(set(found)))


class WbiImg(_Model):
    img_url: str = ""
    sub_url: str = ""


class NavData(_Model):
    """``/x/web-interface/nav`` 的 data。"""

    isLogin: bool = False
    mid: int = 0
    uname: str = ""
    wbi_img: WbiImg | None = None
