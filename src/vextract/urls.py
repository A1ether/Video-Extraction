"""分享文本 → 平台 → 规范化 URL。

用户复制的是分享口令（含中文、表情、多个链接），不是干净的 URL，所以入口必须先做提取。
短链（b23.tv / v.douyin.com / v.kuaishou.com / xhslink.com / t.cn / vm.tiktok.com）
必须解开 302：yt-dlp 的 suitable() 对这些域名只命中 Generic（已实测）。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from .errors import UrlNotFound
from .http import MOBILE_UA

_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\"'<>()（），。；、！？]+")
_TRAILING = "。，,.;；:：!！?？)）]】\"'"

PLATFORM_HOSTS: dict[str, tuple[str, ...]] = {
    "bilibili": ("bilibili.com", "b23.tv", "bili2233.cn"),
    "douyin": ("douyin.com", "iesdouyin.com"),
    "kuaishou": ("kuaishou.com", "gifshow.com", "chenzhongtech.com"),
    "xhs": ("xiaohongshu.com", "xhslink.com", "xhs.link"),
    "weibo": ("weibo.com", "weibo.cn"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
}

SHORT_HOSTS = {
    "b23.tv",
    "v.douyin.com",
    "v.kuaishou.com",
    "xhslink.com",
    "xhs.link",
    "t.cn",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "youtu.be",
}


def extract_url(text: str) -> str:
    """从分享文本里取第一个链接。找不到就抛 UrlNotFound（含提示）。"""
    match = _URL_RE.search(text or "")
    if not match:
        raise UrlNotFound(
            "分享文本里没有找到链接",
            hint="请粘贴完整分享内容（通常形如「7.92 复制打开抖音，看看... https://v.douyin.com/xxxx/」）",
        )
    return match.group(0).rstrip(_TRAILING)


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def platform_of(url: str) -> str | None:
    host = host_of(url)
    for platform, suffixes in PLATFORM_HOSTS.items():
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return platform
    return None


def is_short(url: str) -> bool:
    return host_of(url) in SHORT_HOSTS


async def expand(url: str, client: httpx.AsyncClient | None = None) -> str:
    """解开短链 302，返回最终 URL；失败时原样返回（让 provider 自己再试）。"""
    if not is_short(url):
        return url
    own = client is None
    cli = client or httpx.AsyncClient(
        follow_redirects=True,
        timeout=15,
        headers={"User-Agent": MOBILE_UA},
    )
    try:
        resp = await cli.get(url)
        return str(resp.url)
    except httpx.HTTPError:
        return url
    finally:
        if own:
            await cli.aclose()


def normalize_share_url(text: str) -> tuple[str, str]:
    """返回 (platform, url)。platform 可能为 None（交给 yt-dlp 兜底）。"""
    url = extract_url(text)
    return platform_of(url) or "", url
