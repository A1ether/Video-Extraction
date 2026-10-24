"""统一中间表示：所有 provider 都必须产出 MediaInfo，下游只认它。

这样 CLI / HTTP / 下载器完全不感知平台差异，新增平台不需要改它们。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Quality = Literal["best", "1080p", "720p", "480p", "360p", "audio"]


class Stream(BaseModel):
    """一路可下载的码流。DASH 平台会出现 audio_only=True 的独立音轨。

    rank 由 provider 显式给出（n 越大越好）——不要依赖数组顺序，
    因为平台返回顺序（微博 playback_list）与实际清晰度并不一致。
    """

    url: str
    quality: str
    ext: str = "mp4"
    codec: str | None = None
    audio_only: bool = False
    bitrate: int | None = None
    filesize: int | None = None
    rank: int = 0
    headers: dict[str, str] = Field(default_factory=dict)


class MediaInfo(BaseModel):
    platform: str
    media_id: str
    source_url: str
    title: str
    author: str | None = None
    cover_url: str | None = None
    duration: float | None = None
    stats: dict[str, int] = Field(default_factory=dict)
    streams: list[Stream] = Field(default_factory=list)
    needs_merge: bool = False
    quality_note: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def best_video(self) -> Stream:
        videos = [s for s in self.streams if not s.audio_only]
        if not videos:
            raise ValueError("no video stream")
        return max(videos, key=lambda s: s.rank)

    def best_audio(self) -> Stream | None:
        audios = [s for s in self.streams if s.audio_only]
        return max(audios, key=lambda s: s.rank) if audios else None

    def summary(self) -> str:
        """一行人类可读摘要，CLI 直接用。"""
        bits = [f"{self.platform}", self.title]
        if self.author:
            bits.append(f"@{self.author}")
        if self.duration:
            bits.append(f"{int(self.duration // 60)}:{int(self.duration % 60):02d}")
        bits.append(f"{len(self.streams)} streams")
        return " | ".join(bits)
