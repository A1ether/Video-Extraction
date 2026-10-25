"""统一中间表示：所有 provider 都必须产出 MediaInfo，下游只认它。

这样 CLI / HTTP / 下载器完全不感知平台差异，新增平台不需要改它们。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Quality = Literal["best", "1080p", "720p", "480p", "360p", "audio"]
"""`audio` 不是清晰度档，而是"只要音频"的模式别名（见 download.resolve_mode）。"""

Mode = Literal["video", "audio", "images"]
"""产物模式：视频 / 仅音频 / 图集。None 表示按内容推断。"""

AudioFormat = Literal["m4a", "mp3", "flac"]

TranscriptFormat = Literal["md", "txt", "srt"]


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


class Subtitle(BaseModel):
    """一条字幕轨（B站 CC / AI 字幕）。

    实测（登录态）：`x/player/wbi/v2` 的 `data.subtitle.subtitles[]` 每项给
    `lan` / `lan_doc` / `subtitle_url`，URL 指向一个 JSON：
    `{body: [{from, to, location, content}]}`。`ai-` 前缀是机器生成。
    """

    lan: str
    lan_doc: str = ""
    url: str
    ai: bool = False
    headers: dict[str, str] = Field(default_factory=dict)


class Asset(BaseModel):
    """图文作品里的一张图（抖音/小红书图文笔记、微博配图）。

    与 Stream 分开是因为它不可"选清晰度"、也不参与 ffmpeg 合并：
    图集是"整个作品的一组文件"，不是"同一个视频的多档码流"。
    """

    url: str
    index: int = 1
    """1-based 序号，落盘时用它命名（01.jpg / 02.jpg …）。"""
    ext: str = "jpg"
    width: int | None = None
    height: int | None = None
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
    assets: list[Asset] = Field(default_factory=list)
    """图文作品的图片集。与 streams 互斥：图文的 streams 为空。"""
    subtitles: list[Subtitle] = Field(default_factory=list)
    """可用字幕轨。B站需要登录态（SESSDATA）才会列出——匿名恒为空。"""
    needs_merge: bool = False
    quality_note: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_image_post(self) -> bool:
        """只有图、没有视频流 → 图文作品（抖音/小红书图文笔记）。"""
        return not self.streams and bool(self.assets)

    @property
    def work_kind(self) -> str:
        """给前端/CLI 的一句话类型：视频作品 / 图文作品。"""
        if self.is_image_post:
            return "图文作品"
        return "视频作品"

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
