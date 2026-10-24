"""vextract —— 自媒体视频提取工具。

分享链接/文本 → 统一中间表示（MediaInfo）→ 视频文件 + 元数据。
选型与复用策略见 docs/01-技术调研与选型方案.md。
"""

from __future__ import annotations

from .errors import (
    AuthRequired,
    DependencyMissing,
    DownloadFailed,
    NoMedia,
    RateLimited,
    UnsupportedUrl,
    UpstreamChanged,
    UrlNotFound,
    VextractError,
)
from .models import MediaInfo, Stream
from .registry import extract

__version__ = "0.1.0"

__all__ = [
    "MediaInfo",
    "Stream",
    "extract",
    "VextractError",
    "UrlNotFound",
    "UnsupportedUrl",
    "AuthRequired",
    "RateLimited",
    "UpstreamChanged",
    "NoMedia",
    "DependencyMissing",
    "DownloadFailed",
]