"""HTTP 客户端工厂：统一 UA / 超时 / cookie，并把状态码翻译成本项目错误。"""

from __future__ import annotations

import httpx

from . import cookies
from .errors import RateLimited, UpstreamChanged

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

DEFAULT_HEADERS = {
    "User-Agent": DESKTOP_UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}


def client(
    platform: str,
    *,
    mobile: bool = False,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> httpx.AsyncClient:
    hdrs = dict(DEFAULT_HEADERS)
    if mobile:
        hdrs["User-Agent"] = MOBILE_UA
    if headers:
        hdrs.update(headers)
    jar = cookies.httpx_cookies(platform)
    return httpx.AsyncClient(
        headers=hdrs,
        cookies=jar,
        timeout=timeout,
        follow_redirects=True,
        http2=False,
    )


def check(resp: httpx.Response, platform: str) -> httpx.Response:
    """把平台侧异常翻译成语义化错误。"""
    status = resp.status_code
    if status in (403, 429):
        raise RateLimited(
            f"{platform} 返回 {status}，疑似风控/限流",
            hint="稍后重试；若持续，尝试换网络出口或提供该平台 cookie",
            platform=platform,
        )
    if status >= 500:
        raise UpstreamChanged(
            f"{platform} 返回 {status}",
            hint="平台侧异常，稍后重试；若持续请反馈",
            platform=platform,
        )
    return resp
