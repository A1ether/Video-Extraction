"""运行时配置：环境变量 VEXTRACT_* / .env / 用户设置文件（前端可写）。

优先级：环境变量 > ~/.config/vextract/settings.json > 默认值。
前端设置面板通过 save() 写盘，重启后仍生效。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VEXTRACT_", env_file=".env", extra="ignore")

    home: Path = Path.home() / ".config" / "vextract"
    out_dir: Path = Path.home() / "Downloads"
    timeout: float = 20.0
    cookies_from_browser: str | None = None
    """交给 yt-dlp 的 --cookies-from-browser 值（chrome/firefox/edge/...）。"""
    realtime: bool = True
    """前端默认用 SSE 实时推送进度；关掉则退化为轮询（部分代理/企业网络会打断 EventSource）。"""
    audio_format: str = "m4a"
    """仅音频模式的默认容器（m4a 可与 aac 源零重编码对齐）。"""
    embed_tags: bool = True
    """音频落盘时写入标题/作者/来源标签。"""
    transcript_format: str = "md"
    """本地转录的默认产物格式。"""
    asr_backend: str | None = None
    """ASR 后端；留空按偏好自动挑（mlx_whisper → faster_whisper → whisper）。

    faster-whisper 自带 VAD，非人声音频上比 mlx 少一整类重复幻觉，所以要能选。
    """
    asr_model: str | None = None
    """ASR 模型名；留空用后端默认（mlx-whisper → large-v3-turbo）。"""
    asr_language: str | None = None
    """ASR 语言；留空让模型自己判断。"""

    @property
    def cookie_dir(self) -> Path:
        return self.home / "cookies"

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"


settings = Settings()

BROWSER_CHOICES = ("", "chrome", "edge", "firefox", "safari", "chromium", "brave", "opera", "vivaldi")
AUDIO_FORMATS = ("m4a", "mp3", "flac")
TRANSCRIPT_FORMATS = ("md", "txt", "srt")
ASR_BACKENDS = ("mlx_whisper", "faster_whisper", "whisper")
"""与 asr.BACKENDS 一致；放这里是为了 settings 序列化时不 import 模型库。"""


class SettingsPatch(BaseModel):
    out_dir: str | None = None
    cookies_from_browser: str | None = None
    realtime: bool | None = None
    audio_format: str | None = None
    embed_tags: bool | None = None
    transcript_format: str | None = None
    asr_backend: str | None = None
    asr_model: str | None = None
    asr_language: str | None = None


def set_cookies_from_browser(source: str | None) -> None:
    """CLI 覆盖入口（不进环境变量，避免污染子进程）。"""
    settings.cookies_from_browser = source or None


def load_persisted() -> None:
    """启动时把用户设置文件读进 settings（存在才读，坏了就忽略）。"""
    path = settings.settings_file
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("out_dir"), str) and data["out_dir"]:
        settings.out_dir = Path(data["out_dir"]).expanduser()
    if isinstance(data.get("cookies_from_browser"), str):
        settings.cookies_from_browser = data["cookies_from_browser"] or None
    if isinstance(data.get("realtime"), bool):
        settings.realtime = data["realtime"]
    if data.get("audio_format") in AUDIO_FORMATS:
        settings.audio_format = data["audio_format"]
    if isinstance(data.get("embed_tags"), bool):
        settings.embed_tags = data["embed_tags"]
    if data.get("transcript_format") in TRANSCRIPT_FORMATS:
        settings.transcript_format = data["transcript_format"]
    if isinstance(data.get("asr_backend"), str):
        settings.asr_backend = data["asr_backend"] or None
    if isinstance(data.get("asr_model"), str):
        settings.asr_model = data["asr_model"] or None
    if isinstance(data.get("asr_language"), str):
        settings.asr_language = data["asr_language"] or None


def save(patch: SettingsPatch) -> dict:
    if patch.out_dir:
        settings.out_dir = Path(patch.out_dir).expanduser()
    if patch.cookies_from_browser is not None:
        settings.cookies_from_browser = patch.cookies_from_browser or None
    if patch.realtime is not None:
        settings.realtime = patch.realtime
    if patch.audio_format:
        settings.audio_format = patch.audio_format
    if patch.embed_tags is not None:
        settings.embed_tags = patch.embed_tags
    if patch.transcript_format:
        settings.transcript_format = patch.transcript_format
    if patch.asr_backend is not None:
        settings.asr_backend = patch.asr_backend or None
    if patch.asr_model is not None:
        settings.asr_model = patch.asr_model or None
    if patch.asr_language is not None:
        settings.asr_language = patch.asr_language or None

    settings.home.mkdir(parents=True, exist_ok=True)
    payload = {
        "out_dir": str(settings.out_dir),
        "cookies_from_browser": settings.cookies_from_browser or "",
        "realtime": settings.realtime,
        "audio_format": settings.audio_format,
        "embed_tags": settings.embed_tags,
        "transcript_format": settings.transcript_format,
        "asr_model": settings.asr_model or "",
        "asr_language": settings.asr_language or "",
    }
    settings.settings_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return as_dict()


def as_dict() -> dict:
    return {
        "out_dir": str(settings.out_dir),
        "cookies_from_browser": settings.cookies_from_browser or "",
        "realtime": settings.realtime,
        "audio_format": settings.audio_format,
        "embed_tags": settings.embed_tags,
        "transcript_format": settings.transcript_format,
        "asr_backend": settings.asr_backend or "",
        "asr_model": settings.asr_model or "",
        "asr_language": settings.asr_language or "",
        "asr_backend_choices": ["", *ASR_BACKENDS],
        "cookie_dir": str(settings.cookie_dir),
        "home": str(settings.home),
        "browser_choices": list(BROWSER_CHOICES),
        "audio_formats": list(AUDIO_FORMATS),
        "transcript_formats": list(TRANSCRIPT_FORMATS),
    }