"""小红书：走"零签名"SSR 路线，不碰 x-s / x-t。

理由（见 docs/01 §2.3）：
- 官方向 x-s 签名接口需要 JS signer，无维护良好的许可兼容实现；
- 笔记页的 window.__INITIAL_STATE__ 里直接带 video 直链（含 originVideoKey）；
- 分享文本里的 xsec_token 必须原样保留，否则页面会跳首页（XHS-Downloader issue #174）。

**不学** XHS-Downloader 的 verify=False（它会关掉 TLS 校验）。
"""

from __future__ import annotations

import re

from .. import http
from ..download import rank_of
from ..errors import NoMedia
from ..models import MediaInfo, Stream
from ..parsers import deep_find, extract_js_object, first_key
from .base import Provider

_ID_RE = re.compile(r"/(?:explore|discovery/item|user/profile/[^/]+)/([0-9a-zA-Z]{16,32})")
_CODECS = ("h264", "h265", "av1")


def _is_still_image(url: str) -> bool:
    return url.startswith("http") and "sns" in url and url.lower().endswith((".jpg", ".jpeg", ".webp"))


class Xiaohongshu(Provider):
    name = "xhs"
    priority = 85

    def matches(self, url: str) -> bool:
        return any(h in url for h in ("xiaohongshu.com", "xhslink.com", "xhs.link"))

    async def resolve(self, url: str, *, quality: str = "best") -> MediaInfo:
        token = self._xsec_token(url)
        note_id = self._note_id(url)
        fetch_url = url.split("?")[0]
        if token:
            fetch_url = f"{fetch_url}?xsec_token={token}&xsec_source=pc_feed"
        async with http.client(
            "xhs",
            headers={"Referer": "https://www.xiaohongshu.com/", "Accept": "text/html,application/xhtml+xml"},
        ) as cli:
            resp = await cli.get(fetch_url)
            html = resp.text
            if "/explore" not in str(resp.url) and note_id:
                retry_url = f"https://www.xiaohongshu.com/explore/{note_id}"
                if token:
                    retry_url += f"?xsec_token={token}"
                resp = await cli.get(retry_url)
                html = resp.text

        state = extract_js_object(html, "window.__INITIAL_STATE__=")
        note = self._find_note(state)
        if note is None:
            raise NoMedia(
                "页面里没有笔记数据",
                hint=(
                    "常见原因：① 分享链接里的 xsec_token 丢了（请贴完整分享文本）"
                    "② 笔记被删/仅自己可见 ③ 需要登录（导出 cookie 到 ~/.config/vextract/cookies/xhs.txt）"
                ),
                platform="xhs",
            )

        video = note.get("video") or {}
        streams = self._streams(video)
        if not streams:
            raise NoMedia(
                "这条笔记是图文，没有视频",
                hint="小红书图文请用图集模式（本期未实现）",
                platform="xhs",
            )

        user = note.get("user") or {}
        interact = note.get("interactInfo") or {}
        duration = deep_find(video, lambda n: isinstance(n, dict) and isinstance(n.get("duration"), (int, float))) or {}
        return MediaInfo(
            platform="xhs",
            media_id=note_id or str(first_key(note, "noteId", "id") or ""),
            source_url=url.split("?")[0],
            title=str(first_key(note, "title", "desc") or "xiaohongshu note")[:120],
            author=user.get("nickname") or user.get("nickName"),
            cover_url=self._cover(note, video),
            duration=float(duration["duration"]) if isinstance(duration.get("duration"), (int, float)) else None,
            stats={
                k: int(interact[src])
                for k, src in (
                    ("like", "likedCount"),
                    ("favorite", "collectedCount"),
                    ("comment", "commentCount"),
                    ("repost", "shareCount"),
                )
                if str(interact.get(src, "")).isdigit()
            },
            streams=streams,
            needs_merge=False,
            extra={"xsec_token_present": bool(token)},
        )

    # --- 内部 ---
    @staticmethod
    def _xsec_token(url: str) -> str | None:
        match = re.search(r"[?&]xsec_token=([^&]+)", url)
        return match.group(1) if match else None

    @staticmethod
    def _note_id(url: str) -> str | None:
        match = _ID_RE.search(url)
        return match.group(1) if match else None

    @staticmethod
    def _find_note(state: dict) -> dict | None:
        detail_map = deep_find(state, lambda n: isinstance(n, dict) and "noteDetailMap" in n)
        if isinstance(detail_map, dict):
            for entry in detail_map["noteDetailMap"].values():
                if isinstance(entry, dict) and isinstance(entry.get("note"), dict):
                    return entry["note"]
        data = deep_find(state, lambda n: isinstance(n, dict) and n.get("noteId") and ("desc" in n or "title" in n))
        return data if isinstance(data, dict) else None

    @staticmethod
    def _cover(note: dict, video: dict) -> str | None:
        frame = deep_find(video, lambda n: isinstance(n, str) and _is_still_image(n))
        if frame:
            return frame
        images = note.get("imageList")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            return first_key(images[0], "urlDefault", "urlPre", "url")
        return None

    @staticmethod
    def _streams(video: dict) -> list[Stream]:
        out: list[Stream] = []
        stream_block = deep_find(video, lambda n: isinstance(n, dict) and any(k in n for k in _CODECS))
        if isinstance(stream_block, dict):
            for codec_name in _CODECS:
                items = stream_block.get(codec_name)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = first_key(item, "masterUrl", "backupUrls")
                    if isinstance(url, list):
                        url = url[0] if url else None
                    if not url:
                        continue
                    height = item.get("height")
                    bitrate = item.get("videoBitrate") or item.get("avgBitrate")
                    label = f"{height}p" if height else f"{codec_name}"
                    out.append(
                        Stream(
                            url=str(url),
                            quality=label,
                            codec=codec_name,
                            ext="mp4",
                            bitrate=bitrate,
                            rank=rank_of(label, int(height or 0), int(bitrate or 0)),
                            headers={"Referer": "https://www.xiaohongshu.com/"},
                        )
                    )
        origin = deep_find(video, lambda n: isinstance(n, dict) and isinstance(n.get("originVideoKey"), str))
        if isinstance(origin, dict) and origin.get("originVideoKey"):
            out.append(
                Stream(
                    url=f"https://sns-video-bd.xhscdn.com/{origin['originVideoKey']}",
                    quality="origin",
                    codec="origin",
                    ext="mp4",
                    rank=rank_of("origin"),
                    headers={"Referer": "https://www.xiaohongshu.com/"},
                )
            )
        seen: set[str] = set()
        unique: list[Stream] = []
        for stream in sorted(out, key=lambda s: s.rank, reverse=True):
            if stream.url in seen:
                continue
            seen.add(stream.url)
            unique.append(stream)
        return unique[:6]