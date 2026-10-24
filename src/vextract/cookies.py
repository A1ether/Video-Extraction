"""cookie 通道：只做"读用户给的 cookie"，不做登录、不碰浏览器数据库。

阶梯（docs/01 §6.3）：
  L0 无 cookie
  L1 ~/.config/vextract/cookies/<platform>.txt  (Netscape 格式，与 yt-dlp --cookies 通用)
  L2 由调用方传 --cookies-from-browser（转交 yt-dlp）
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

COOKIE_DIR = Path(os.environ.get("VEXTRACT_HOME", Path.home() / ".config" / "vextract")) / "cookies"


def cookie_file(platform: str) -> Path | None:
    path = COOKIE_DIR / f"{platform}.txt"
    return path if path.exists() else None


def load_netscape(path: Path) -> list[tuple[str, str, str]]:
    """解析 Netscape cookie 文件 → [(domain, name, value)]。"""
    rows: list[tuple[str, str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            rows.append((parts[0], parts[5], parts[6]))
    return rows


def httpx_cookies(platform: str) -> httpx.Cookies | None:
    path = cookie_file(platform)
    if not path:
        return None
    jar = httpx.Cookies()
    for domain, name, value in load_netscape(path):
        jar.set(name, value, domain=domain.lstrip("."), path="/")
    return jar


def ytdlp_cookiefile(platform: str) -> str | None:
    path = cookie_file(platform)
    return str(path) if path else None


def describe(platform: str) -> str:
    path = cookie_file(platform)
    return f"已加载 {path}" if path else f"未找到 {COOKIE_DIR / f'{platform}.txt'}"
