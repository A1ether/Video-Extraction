"""Provider 协议：一个平台一个文件，两个方法。

新增平台只需在 providers/ 下加一个文件，注册表自动发现（无需改路由/无需改 CLI）。
优先级 priority 越大越先被询问；yt-dlp 兜底 provider 优先级最低。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import MediaInfo


class Provider(ABC):
    name: str = "base"
    priority: int = 0

    @abstractmethod
    def matches(self, url: str) -> bool:
        """能否处理这个（已展开短链的）URL。"""

    @abstractmethod
    async def resolve(self, url: str, *, quality: str = "best") -> MediaInfo:
        """解析出统一中间表示。失败必须抛 errors.VextractError 的子类。"""