"""兜底 provider：YouTube / TikTok / 其余 yt-dlp 覆盖的 1700+ 站点。

优先级最低（10），所以只有在前面的自研 provider 都不认领时才轮到它。
"""

from __future__ import annotations

from .. import config, cookies, engine
from ..errors import UnsupportedUrl
from ..models import MediaInfo
from ..urls import platform_of
from .base import Provider


class YtdlpGeneric(Provider):
    name = "generic"
    priority = 10

    def matches(self, url: str) -> bool:
        return url.startswith("http://") or url.startswith("https://")

    async def resolve(self, url: str, *, quality: str = "best") -> MediaInfo:
        platform = platform_of(url) or ""
        cookiefile = cookies.ytdlp_cookiefile(platform) if platform else None
        info = await engine.extract_with_ytdlp(
            url,
            platform=platform,
            cookiefile=cookiefile,
            from_browser=config.settings.cookies_from_browser,
        )
        if info.streams:
            return info
        raise UnsupportedUrl("该站点无可用码流", hint="用 `vx platforms` 查看已支持平台", platform=platform)