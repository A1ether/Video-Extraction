"""统一错误层：每个错误都带机器可读 code + 人类可执行的 hint。

设计原则（见 docs/01-技术调研与选型方案.md §6.4）：
- code 给 HTTP 客户端/AI 用来分支，hint 给用户用来"下一步该干什么"。
- 绝不把平台原始报错直接抛给用户，必须翻译成本项目语义。
"""

from __future__ import annotations

from typing import Any


class VextractError(Exception):
    """所有可预期错误的基类。"""

    code: str = "VEXTRACT_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        platform: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.platform = platform
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.platform:
            out["platform"] = self.platform
        if self.hint:
            out["hint"] = self.hint
        if self.detail:
            out["detail"] = self.detail
        return out

    def render(self) -> str:
        """CLI 用：错误 + 提示两行。"""
        head = f"[{self.code}] {self.message}"
        return f"{head}\n  → {self.hint}" if self.hint else head


class UrlNotFound(VextractError):
    code = "URL_NOT_FOUND"
    http_status = 400


class UnsupportedUrl(VextractError):
    code = "UNSUPPORTED_URL"
    http_status = 400


class AuthRequired(VextractError):
    code = "AUTH_REQUIRED"
    http_status = 401


class RateLimited(VextractError):
    code = "RATE_LIMITED"
    http_status = 429


class UpstreamChanged(VextractError):
    code = "UPSTREAM_CHANGED"
    http_status = 502


class NoMedia(VextractError):
    code = "NO_MEDIA"
    http_status = 422


class DependencyMissing(VextractError):
    code = "DEPENDENCY_MISSING"
    http_status = 500


class DownloadFailed(VextractError):
    code = "DOWNLOAD_FAILED"
    http_status = 500


class Cancelled(VextractError):
    code = "CANCELLED"
    http_status = 499
