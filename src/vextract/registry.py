"""provider 自动发现与分发。

约定：providers/ 包内任何 Provider 子类都会被注册，按 priority 降序询问。
所以"新增平台"= 新增一个文件 + 一个类，不动这里。
"""

from __future__ import annotations

import importlib
import pkgutil
from functools import lru_cache

from . import urls as urlutil
from .errors import UnsupportedUrl
from .models import MediaInfo
from .providers.base import Provider


@lru_cache(maxsize=1)
def all_providers() -> tuple[Provider, ...]:
    pkg = importlib.import_module(".providers", __package__)
    found: list[Provider] = []
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        module = importlib.import_module(f"{pkg.__name__}.{mod.name}")
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, Provider)
                and obj is not Provider
                and obj.__module__ == module.__name__
            ):
                found.append(obj())
    found.sort(key=lambda p: -p.priority)
    return tuple(found)


def pick(url: str) -> Provider | None:
    for provider in all_providers():
        if provider.matches(url):
            return provider
    return None


def platform_list() -> list[tuple[str, str, int]]:
    return [(p.name, type(p).__module__.rsplit(".", 1)[-1], p.priority) for p in all_providers()]


async def extract(text: str, *, quality: str = "best") -> MediaInfo:
    """入口：分享文本/URL → MediaInfo。"""
    url = urlutil.extract_url(text)
    expanded = await urlutil.expand(url)
    provider = pick(expanded) or pick(url)
    if provider is None:
        raise UnsupportedUrl(
            f"没有 provider 认领这个链接：{url}",
            hint="当前支持 抖音/快手/小红书/B站/微博/YouTube/TikTok 及 yt-dlp 覆盖的 1700+ 站点；"
            "用 `vx platforms` 查看",
        )
    return await provider.resolve(expanded, quality=quality)