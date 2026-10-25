"""HTTP 客户端工厂：统一 UA / 超时 / cookie，并把状态码翻译成本项目错误。"""

from __future__ import annotations

import httpx

from . import config, cookies
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


def browser_cookies(platform: str) -> httpx.Cookies | None:
    """从用户本机浏览器只读取这个平台的 cookie（只读，不写、不落盘、不打印）。

    为什么需要它：设置页的「从浏览器读取 cookie」原先只传给 yt-dlp，
    自研 provider（B站/抖音/小红书…）走的是这个 httpx 工厂，根本读不到——
    于是"登录后才有的东西"（B站 CC 字幕、高码率）对只用浏览器登录的用户不可达。
    """
    source = config.settings.cookies_from_browser
    if not source:
        return None
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        return None
    try:
        jar = extract_cookies_from_browser(source)
    except Exception:  # noqa: BLE001 — 浏览器没装/库被锁/钥匙串拒绝，都当作"没有"
        return None

    wanted = {
        "bilibili": (".bilibili.com", "bilibili.com"),
        "douyin": (".douyin.com", "douyin.com"),
        "xhs": (".xiaohongshu.com", "xiaohongshu.com"),
        "kuaishou": (".kuaishou.com", "kuaishou.com"),
        "weibo": (".weibo.com", "weibo.com", ".weibo.cn"),
    }.get(platform, ())
    if not wanted:
        return None

    out = httpx.Cookies()
    found = 0
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if not domain:
            continue
        if not any(domain == item.lstrip(".") or domain.endswith("." + item.lstrip(".")) for item in wanted):
            continue
        out.set(cookie.name, cookie.value or "", domain=domain, path=cookie.path or "/")
        found += 1
    return out if found else None


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
    # 优先用 L1 文件；没有文件再看设置里的"从浏览器读取"（L2）
    jar = cookies.httpx_cookies(platform) or browser_cookies(platform)
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
