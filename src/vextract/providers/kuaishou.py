"""快手：yt-dlp 零覆盖（`--list-extractors` 里 kuaishou 命中 0），必须自研。

实测演进记录（2026-10-25，本机）：
- 桌面页 `www.kuaishou.com/short-video/<id>` 的 `window.__APOLLO_STATE__` **只剩 UI 配置**
  （typnames 只有 VisionTab/Banner/VisionConfigResult），`VisionVideoDetailPhoto` 出现 0 次
  → 流传很广的"读桌面 apollo state"写法已失效（videodl 也因此把它降级为第二方案）。
- 桌面 GraphQL `/graphql` + 仅 `did` cookie → 返回 `result:400002` 并要求过
  captcha.zt.kuaishou.com 验证码 → 无浏览器不可行。
- **可用链路**：移动页 `https://m.gifshow.com/fw/photo/<id>`，数据在 `window.INIT_STATE` 里，
  顶层键做了 -1 凯撒位移（值是明文），取 photo/simple/info 那一项即可拿到
  caption / duration / userName / mainMvUrls / manifest.adaptationSet[].representation[]。
"""

from __future__ import annotations

import json
import re

from .. import http
from ..download import rank_of
from ..errors import NoMedia, UpstreamChanged
from ..models import MediaInfo, Stream
from ..parsers import balanced_object, deep_find, extract_js_object, first_key
from .base import Provider

_ID_RES = (
    re.compile(r"/short-video/([0-9A-Za-z_-]+)"),
    re.compile(r"/fw/photo/([0-9A-Za-z_-]+)"),
    re.compile(r"[?&]photoId=([0-9A-Za-z_-]+)"),
)
_MOBILE_PAGE = "https://m.gifshow.com/fw/photo/{pid}"


def photo_id(url: str) -> str | None:
    for pattern in _ID_RES:
        match = pattern.search(url)
        if match:
            return match.group(1)
    tail = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return tail or None


def unshift(text: str) -> str:
    """INIT_STATE 顶层键的 -1 位移还原（仅用于识别端点，值本身是明文）。"""
    return "".join(chr(ord(c) - 1) if 32 <= ord(c) < 127 else c for c in text)


def parse_init_state(html: str) -> dict:
    marker = "window.INIT_STATE"
    idx = html.find(marker)
    if idx < 0:
        raise UpstreamChanged(
            "移动页里找不到 window.INIT_STATE（快手可能已改版）",
            hint="请反馈该链接（附 --debug）",
            platform="kuaishou",
        )
    brace = html.find("{", idx)
    if brace < 0:
        raise UpstreamChanged("INIT_STATE 后没有对象字面量", hint="反馈该链接", platform="kuaishou")
    return json.loads(balanced_object(html, brace))


def find_photo(state: dict) -> dict | None:
    """按端点名定位作品节点；端点名被位移过，所以先还原再匹配。"""
    for key, value in state.items():
        decoded = unshift(key)
        if isinstance(value, dict) and "photo/simple/info" in decoded and isinstance(value.get("photo"), dict):
            return value["photo"]
    # 兜底：不依赖键名，直接在值里找带 mainMvUrls 的节点
    found = deep_find(
        state,
        lambda n: isinstance(n, dict) and ("mainMvUrls" in n or "caption" in n) and ("duration" in n or "videoId" in n),
    )
    return found if isinstance(found, dict) else None


class Kuaishou(Provider):
    name = "kuaishou"
    priority = 85

    def matches(self, url: str) -> bool:
        return any(h in url for h in ("kuaishou.com", "gifshow.com", "chenzhongtech.com"))

    async def resolve(self, url: str, *, quality: str = "best") -> MediaInfo:
        pid = photo_id(url)
        if not pid:
            raise NoMedia(
                "没识别出快手作品 ID",
                hint="请用 https://www.kuaishou.com/short-video/<id> 或 https://v.kuaishou.com/<短码> 链接",
                platform="kuaishou",
            )

        async with http.client("kuaishou", mobile=True, headers={"Referer": "https://v.kuaishou.com/"}) as cli:
            resp = await cli.get(_MOBILE_PAGE.format(pid=pid))
            html = resp.text

        photo = find_photo(parse_init_state(html))
        if photo is None:
            # 桌面 SSR 兜底（历史上可用，保留以免上游回滚时无路可走）
            photo = self._from_desktop(html)
        if photo is None:
            raise NoMedia(
                "页面里没有视频节点（作品可能已删除/仅粉丝可见/需要登录）",
                hint=(
                    "确认链接在浏览器能正常播放；若需登录，导出 cookie 到 "
                    "~/.config/vextract/cookies/kuaishou.txt"
                ),
                platform="kuaishou",
            )

        streams = self._streams(photo)
        if not streams:
            raise NoMedia("快手作品没有可用码流（可能是图文）", hint="确认链接指向视频作品", platform="kuaishou")

        duration = first_key(photo, "duration")
        cover = photo.get("coverUrls") or photo.get("webpCoverUrls")
        cover_url = None
        if isinstance(cover, list) and cover:
            cover_url = cover[0].get("url") if isinstance(cover[0], dict) else str(cover[0])
        stats = {
            key: int(photo[src])
            for key, src in (
                ("view", "viewCount"),
                ("like", "likeCount"),
                ("comment", "commentCount"),
                ("repost", "forwardCount"),
            )
            if isinstance(photo.get(src), int)
        }
        return MediaInfo(
            platform="kuaishou",
            media_id=pid,
            source_url=f"https://www.kuaishou.com/short-video/{pid}",
            title=str(first_key(photo, "caption", "title") or f"kuaishou-{pid}"),
            author=first_key(photo, "userName", "userNameText", "authorName"),
            cover_url=cover_url,
            duration=float(duration) / 1000 if isinstance(duration, (int, float)) and duration > 100 else None,
            stats=stats,
            streams=streams,
            needs_merge=False,
            extra={"userId": first_key(photo, "userId"), "videoId": first_key(photo, "videoId")},
        )

    # --- 内部 ---
    @staticmethod
    def _from_desktop(html: str) -> dict | None:
        try:
            state = extract_js_object(html, "window.__APOLLO_STATE__")
        except Exception:  # noqa: BLE001 — 兜底路径失败即视为无数据
            return None
        client = state.get("defaultClient") or {}
        if isinstance(client, dict):
            for value in client.values():
                if isinstance(value, dict) and value.get("__typename") == "VisionVideoDetailPhoto":
                    return value
        return None

    @staticmethod
    def _streams(photo: dict) -> list[Stream]:
        headers = {"Referer": "https://www.kuaishou.com/"}
        out: list[Stream] = []

        manifest = photo.get("manifest") or {}
        for adaptation in manifest.get("adaptationSet") or []:
            if not isinstance(adaptation, dict):
                continue
            for rep in adaptation.get("representation") or []:
                if not isinstance(rep, dict) or not rep.get("url"):
                    continue
                height = rep.get("height") or 0
                label = rep.get("qualityLabel") or (f"{height}p" if height else "video")
                bitrate = rep.get("avgBitrate")
                out.append(
                    Stream(
                        url=rep["url"],
                        quality=str(label),
                        codec=rep.get("videoCodec"),
                        ext="mp4",
                        bitrate=int(bitrate) * 1000 if bitrate else None,
                        filesize=rep.get("fileSize"),
                        rank=rank_of(str(label), int(height), int(bitrate or 0) * 1000),
                        headers=headers,
                    )
                )

        for entry in photo.get("mainMvUrls") or []:
            if isinstance(entry, dict) and entry.get("url"):
                out.append(
                    Stream(
                        url=entry["url"],
                        quality="origin",
                        ext="mp4",
                        rank=rank_of("origin"),
                        headers=headers,
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