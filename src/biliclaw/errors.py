"""错误类型、退出码，以及 B站错误码 → 用户可行动提示的映射。

分别对应需求文档的 FR-11（退出码约定）与 FR-12（错误处理与用户提示）。
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """进程退出码约定（需求文档 FR-11）。"""

    OK = 0
    PARTIAL_FAILURE = 1
    USAGE = 2
    NETWORK = 3
    AUTH = 4
    RISK_CONTROL = 5
    MISSING_DEPENDENCY = 6
    FILESYSTEM = 7


class BiliError(Exception):
    """所有可预期错误的基类。

    携带面向用户的 ``message`` 与可选的 ``hint``（建议动作）。
    ``render()`` 产出最终展示给用户的文本；异常链与堆栈只在 --debug 时输出。
    """

    exit_code: ExitCode = ExitCode.NETWORK

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        if self.hint:
            return f"{self.message}\n  → {self.hint}"
        return self.message


class UsageError(BiliError):
    """命令本身写错了：选项互斥、取值不认识、缺参数。

    ``cli.main`` 会给这类错误补打一整页帮助——用户真正需要的是"到底有哪些选项"。
    所以继承本类要谨慎：这里的判断标准是**命令敲对了没有**，而不是"退出码是不是 2"。
    """

    exit_code = ExitCode.USAGE


class ContentUnavailableError(BiliError):
    """要的东西不在：稿件下架、这支视频没有那一档清晰度。

    退出码沿用 ``USAGE``（需求文档的 0–7 里只有这一格能表达"你要的东西不可用"），
    但它**刻意不是 :class:`UsageError`**：用户命令敲得完全正确，只是这支视频没给到
    他要的那一档。把整页选项表糊在这种错误后面，等于把矛头指向用户自己。
    """

    exit_code = ExitCode.USAGE


class VideoUnavailableError(ContentUnavailableError):
    """稿件不存在、不可见或审核中。"""


class QualityUnavailableError(ContentUnavailableError):
    """严格模式下请求的清晰度不可用。"""


class AuthError(BiliError):
    """需要登录，或权限不足（含大会员专享）。"""

    exit_code = ExitCode.AUTH


class RiskControlError(BiliError):
    """触发风控（HTTP 412 或业务码 -352）。"""

    exit_code = ExitCode.RISK_CONTROL


class NetworkError(BiliError):
    """网络层面的失败（重试后仍不成功）。"""

    exit_code = ExitCode.NETWORK


class LinkExpiredError(NetworkError):
    """CDN 链接过期（约 2 小时有效期），需要重新请求 playurl 换链（FR-04.3）。"""


class MissingDependencyError(BiliError):
    """缺少外部依赖，如 ffmpeg。"""

    exit_code = ExitCode.MISSING_DEPENDENCY


class FileSystemError(BiliError):
    """磁盘或文件系统错误。"""

    exit_code = ExitCode.FILESYSTEM


class ApiSchemaError(BiliError):
    """接口返回结构与预期不符——B站接口变更的早期信号。"""

    exit_code = ExitCode.NETWORK


#: B站业务错误码 → (面向用户的说明, 建议动作)。
#: 来源：需求文档 FR-12 的错误映射表。
API_ERROR_HINTS: dict[int, tuple[str, str | None]] = {
    -400: ("请求参数错误", "这通常意味着接口调用方式需要更新"),
    -403: ("权限不足", "该内容需要登录或更高权限"),
    -404: ("视频不存在", "检查链接或 ID 是否有效，视频可能已被删除"),
    -352: ("风控校验失败", "降低并发或加限速，稍后重试；也可能是 Cookie 失效"),
    -10403: ("大会员专享内容", "该清晰度需要大会员，可改用较低档位"),
    62002: ("稿件不可见", "该稿件当前无法访问（可能已删除或设为私密）"),
    62004: ("稿件审核中", "该稿件当前无法访问，请稍后再试"),
    87007: ("充电专属视频", "本项目不支持下载充电专属内容"),
    87008: ("充电专属视频", "本项目不支持下载充电专属内容"),
}

#: 这些业务码对应的错误类型（决定退出码）。
_AUTH_CODES = {-403, -10403}
_USAGE_CODES = {-400, -404, 62002, 62004, 87007, 87008}
_RISK_CODES = {-352}


def error_from_api_code(code: int, message: str = "") -> BiliError:
    """把 B站业务错误码转换为带用户提示的异常。"""
    description, hint = API_ERROR_HINTS.get(code, (message or f"接口返回错误码 {code}", None))

    if code in _RISK_CODES:
        return RiskControlError(description, hint=hint)
    if code in _AUTH_CODES:
        return AuthError(description, hint=hint)
    if code in _USAGE_CODES:
        if code in {62002, 62004}:
            return VideoUnavailableError(description, hint=hint)
        if code in {87007, 87008}:
            return UsageError(description, hint=hint)
        return UsageError(description, hint=hint)
    return BiliError(description, hint=hint)
