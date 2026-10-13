"""B站视频下载器（M1）。

模块划分：
    errors      错误类型、退出码、B站错误码 → 用户提示映射
    models      API 返回数据的结构化模型
    cookies     登录态加载与脱敏
    wbi         WBI 请求签名
    api         B站接口客户端（唯一的接口耦合点）
    quality     清晰度档位解析与流选择（FR-03 核心）
    naming      文件名/目录模板渲染与净化
    downloader  分片并发下载、断点续传、换链重试
    muxer       ffmpeg 无损合并与校验
    progress    终端进度展示
    cli         命令行入口
"""

__version__ = "0.1.0"
