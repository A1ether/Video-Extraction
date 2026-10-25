"""抖音：唯一走 yt-dlp 的国内平台 —— 因为签名/身份风控已经不划算自己扛。

实测事实（2026-10-25，见 docs/01 §4.1）：
- yt-dlp DouyinIE 在无 cookie 时报 `Fresh cookies (not necessarily logged in) are needed`；
- 老项目的"免 cookie 分享页"路线已死：iesdouyin 分享页里 `videoInfoRes` 出现 0 次；
- 只塞一个从 ttwid 注册接口拿的 ttwid 也不够；
- **补上完整 cookie 也不够**：拿 70 条登录态 cookie（含 UIFID/sessionid/ttwid）实测，
  详情接口仍 403 `Blocked by ArgusSecurityPlugin`。风控是两道闸：
    ① `uifid`：cookie 里的 UIFID 必须以**查询参数**回传 → 缺了报 `Uifid Not Found`；
    ② `a_bogus` 加签参数 → 缺了报 `Signature Not Found`（补上 uifid 之后才露出来）。
  yt-dlp 的 DouyinIE 两个都不发，源码里就写着 TODO "Run verification challenge code"。

所以本 provider 的价值在"把真实闸门说清楚"，而不是自己造签名。
"""

from __future__ import annotations

from .. import config, cookies, engine
from ..errors import AuthRequired
from ..models import MediaInfo
from .base import Provider

ARGUS_HINT = (
    "抖音的 Argus 风控是两道闸，cookie 只够第一道：\n"
    "  ① cookie 里的 UIFID 必须以查询参数回传（yt-dlp 不发）\n"
    "  ② 还缺 a_bogus 加签参数 —— yt-dlp 的 DouyinIE 不实现（源码里是 TODO）\n"
    "  实测：带上完整登录 cookie 直接打详情接口，仍 403 `Signature Not Found`。\n"
    "  出路：换第三方解析接口兜底，或本地复现 a_bogus 后改走自研 provider（尚未实现）"
)


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
                    "L1：用浏览器插件（Cookie-Editor / Get cookies.txt）导出，"
                    "再 `vx cookies import <导出文件>`（默认写 douyin.txt）\n"
                    "L2：或直接 `vx get <链接> --cookies-from-browser chrome`（读你本机 Chrome 的 cookie）"
                ),
                platform="douyin",
            )
        try:
            return await engine.extract_with_ytdlp(
                url,
                platform="douyin",
                cookiefile=cookiefile,
                from_browser=from_browser,
            )
        except AuthRequired as exc:
            # cookie 都给了还被拒 —— 别让用户继续折腾 cookie：这里是签名闸门，不是身份问题。
            raise AuthRequired(
                "抖音仍被 Argus 风控拒绝（cookie 已加载，缺的是签名参数）",
                hint=ARGUS_HINT,
                platform="douyin",
                detail=exc.detail,
            ) from exc