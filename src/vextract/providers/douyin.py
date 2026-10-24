"""抖音：唯一走 yt-dlp 的国内平台 —— 因为签名/身份风控已经不划算自己扛。

实测事实（2026-10-25，见 docs/01 §4.1）：
- yt-dlp DouyinIE 在无 cookie 时报 `Fresh cookies (not necessarily logged in) are needed`；
- 老项目的"免 cookie 分享页"路线已死：iesdouyin 分享页里 `videoInfoRes` 出现 0 次；
- 只塞一个从 ttwid 注册接口拿的 ttwid 也不够。

所以本 provider 的价值在"把 cookie 阶梯做成可执行的指引"，而不是自己造签名。
"""

from __future__ import annotations

from .. import config, cookies, engine
from ..errors import AuthRequired
from ..models import MediaInfo
from .base import Provider


class Douyin(Provider):
    name = "douyin"
    priority = 88

    def matches(self, url: str) -> bool:
        return "douyin.com" in url or "iesdouyin.com" in url

    async def resolve(self, url: str, *, quality: str = "best") -> MediaInfo:
        cookiefile = cookies.ytdlp_cookiefile("douyin")
        from_browser = config.settings.cookies_from_browser
        if not cookiefile and not from_browser:
            raise AuthRequired(
                "抖音必须提供浏览器 cookie（匿名直连已被风控挡住）",
                hint=(
                    "L1：用浏览器插件（如 Get cookies.txt）导出 Netscape 格式，"
                    f"存到 {config.settings.cookie_dir / 'douyin.txt'}\n"
                    "L2：或直接 `vx get <链接> --cookies-from-browser chrome`（读你本机 Chrome 的 cookie）"
                ),
                platform="douyin",
            )
        return await engine.extract_with_ytdlp(
            url,
            platform="douyin",
            cookiefile=cookiefile,
            from_browser=from_browser,
        )