"""yt-dlp 引擎适配：唯一允许直接接触 yt-dlp 的地方。

决策依据（ADR-001）：库内嵌而不是 subprocess —— 官方 README 明确 stdout 格式不可依赖。
yt-dlp 是同步 API，所以放进线程池，保持上游调用点是 async。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from .errors import AuthRequired, DownloadFailed, NoMedia, UnsupportedUrl, UpstreamChanged
from .models import MediaInfo, Stream

_QUALITY_BY_HEIGHT = {
    4320: "8K",
    2160: "4K",
    1440: "2K",
    1080: "1080p",
    720: "720p",
    480: "480p",
    360: "360p",
    240: "240p",
}


def _extract_sync(url: str, cookiefile: str | None, from_browser: str | None) -> dict[str, Any]:
    import yt_dlp

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_args": {"bilibili": {"prefer_multi_flv": ["false"]}},
    }
    snapshot = None
    if cookiefile:
        # yt-dlp 的 cookie jar 在退出时会 save()，会**改写用户这份文件**（实测：一次运行就
        # 把 70 条削成 66 条，丢掉 sdk_source_info / bit_env 这类它不认识的条目）。
        # 我们的契约是"只读用户的 cookie"，所以给它一份临时副本，原文件一个字节都不动。
        cookiefile, snapshot = _shadow_cookiefile(cookiefile)
        opts["cookiefile"] = cookiefile
    if from_browser:
        opts["cookiesfrombrowser"] = (from_browser, None, None, None)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                raise UpstreamChanged("yt-dlp 未返回任何数据", hint="稍后重试或反馈该链接", detail=url)
            return ydl.sanitize_info(info)
    finally:
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)


def _shadow_cookiefile(source: str) -> tuple[str, Path]:
    """把用户 cookie 复制到临时文件，交给 yt-dlp 随便改写；返回 (临时路径, 待删句柄)。"""
    original = Path(source)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 — 生命周期跨到调用方 finally
        prefix="vx-cookies-", suffix=".txt", delete=False
    )
    try:
        handle.write(original.read_bytes())
        handle.flush()
        handle.close()
        shadow = Path(handle.name)
        shadow.chmod(0o600)  # 里面有 cookie，别落成 0644
    except OSError:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise
    return str(shadow), shadow


def _translate(exc: Exception, url: str) -> Exception:
    text = str(exc)
    low = text.lower()
    if "fresh cookies" in low or "login" in low or "sign in" in low or "account" in low:
        return AuthRequired(
            "该平台要求浏览器 cookie（不是登录态也常常需要）",
            hint=(
                "二选一：\n"
                "  ① 用浏览器插件导出 Netscape cookie，存到 ~/.config/vextract/cookies/<platform>.txt\n"
                "  ② 传 --cookies-from-browser chrome|edge|firefox 让 yt-dlp 直接读浏览器"
            ),
            detail=text.splitlines()[0] if text else None,
        )
    if "unsupported url" in low:
        return UnsupportedUrl("yt-dlp 不支持该链接", hint="用 `vx platforms` 查看已支持平台", detail=text[:200])
    if "no video formats found" in low or "no formats" in low:
        return NoMedia("该条内容没有可下载的视频流（可能是图文）", hint="确认链接指向视频", detail=text[:200])
    if "403" in low or "429" in low or "too many requests" in low:
        return AuthRequired("平台拒绝访问（403/429）", hint="换网络出口后重试，或提供 cookie", detail=text[:200])
    return UpstreamChanged(
        "yt-dlp 解析失败（平台可能已改版）",
        hint="先升级引擎：`uv add -U yt-dlp`；仍失败请附带 --debug 输出反馈",
        detail=text.splitlines()[0] if text else None,
    )


def _label(fmt: dict[str, Any], audio_only: bool) -> str:
    if audio_only:
        abr = fmt.get("abr")
        return f"audio-{int(abr)}k" if abr else "audio"
    height = fmt.get("height")
    note = fmt.get("format_note") or ""
    base = _QUALITY_BY_HEIGHT.get(height or 0, f"{height}p" if height else "video")
    return f"{base}{' ' + note if note and note not in base else ''}"


def _to_streams(info: dict[str, Any]) -> list[Stream]:
    from .download import rank_of

    out: list[Stream] = []
    for fmt in info.get("formats") or []:
        if not fmt.get("url"):
            continue
        vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
        audio_only = (not vcodec or vcodec == "none") and bool(acodec and acodec != "none")
        if vcodec and vcodec != "none":
            audio_only = False
        if not vcodec and not acodec:
            continue
        label = _label(fmt, audio_only)
        height = int(fmt.get("height") or 0)
        bitrate = int(fmt.get("abr") or 0) if audio_only else int(fmt.get("tbr") or 0) * 1000
        out.append(
            Stream(
                url=fmt["url"],
                quality=label,
                ext=fmt.get("ext") or ("m4a" if audio_only else "mp4"),
                codec=vcodec if not audio_only else acodec,
                audio_only=audio_only,
                bitrate=bitrate or None,
                filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
                rank=rank_of(label, height, bitrate),
            )
        )
    videos = sorted([s for s in out if not s.audio_only], key=lambda s: s.rank, reverse=True)
    audios = sorted([s for s in out if s.audio_only], key=lambda s: s.rank, reverse=True)
    return videos[:6] + audios[:3]


async def extract_with_ytdlp(
    url: str,
    *,
    platform: str,
    cookiefile: str | None = None,
    from_browser: str | None = None,
) -> MediaInfo:
    try:
        info = await asyncio.to_thread(_extract_sync, url, cookiefile, from_browser)
    except Exception as exc:  # noqa: BLE001 — 统一翻译成语义化错误
        raise _translate(exc, url) from exc

    streams = _to_streams(info)
    if not streams:
        raise NoMedia(
            "该链接没有可下载的码流",
            hint="可能是图文/直播回放；或链接已失效",
            platform=platform,
            detail=info.get("extractor"),
        )
    video = next((s for s in streams if not s.audio_only), None)
    audio = next((s for s in streams if s.audio_only), None)
    needs_merge = bool(video and audio and not any(
        s for s in streams if not s.audio_only and "audio" in str(s.codec).lower()
    ))

    stats: dict[str, int] = {}
    for key, src in (
        ("view", "view_count"),
        ("like", "like_count"),
        ("comment", "comment_count"),
        ("repost", "repost_count"),
    ):
        value = info.get(src)
        if isinstance(value, int):
            stats[key] = value

    return MediaInfo(
        platform=platform or info.get("extractor_key") or "generic",
        media_id=str(info.get("id") or ""),
        source_url=url,
        title=str(info.get("title") or "untitled"),
        author=info.get("uploader") or info.get("channel"),
        cover_url=info.get("thumbnail"),
        duration=float(info["duration"]) if isinstance(info.get("duration"), (int, float)) else None,
        stats=stats,
        streams=streams,
        needs_merge=needs_merge,
        quality_note=None,
        extra={"extractor": info.get("extractor"), "webpage": info.get("webpage_url")},
    )


def engine_version() -> str:
    import yt_dlp

    return yt_dlp.version.__version__


__all__ = ["extract_with_ytdlp", "engine_version", "DownloadFailed"]