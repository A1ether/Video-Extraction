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

    @property
    def cookie_dir(self) -> Path:
        return self.home / "cookies"

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"


settings = Settings()

BROWSER_CHOICES = ("", "chrome", "edge", "firefox", "safari", "chromium", "brave", "opera", "vivaldi")


class SettingsPatch(BaseModel):
    out_dir: str | None = None
    cookies_from_browser: str | None = None
    realtime: bool | None = None


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


def save(patch: SettingsPatch) -> dict:
    if patch.out_dir:
        settings.out_dir = Path(patch.out_dir).expanduser()
    if patch.cookies_from_browser is not None:
        settings.cookies_from_browser = patch.cookies_from_browser or None
    if patch.realtime is not None:
        settings.realtime = patch.realtime

    settings.home.mkdir(parents=True, exist_ok=True)
    payload = {
        "out_dir": str(settings.out_dir),
        "cookies_from_browser": settings.cookies_from_browser or "",
        "realtime": settings.realtime,
    }
    settings.settings_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return as_dict()


def as_dict() -> dict:
    return {
        "out_dir": str(settings.out_dir),
        "cookies_from_browser": settings.cookies_from_browser or "",
        "realtime": settings.realtime,
        "cookie_dir": str(settings.cookie_dir),
        "home": str(settings.home),
        "browser_choices": list(BROWSER_CHOICES),
    }