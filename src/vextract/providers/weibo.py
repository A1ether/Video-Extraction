"""微博：游客墙 + statuses/show（单 mp4 直链，无 DASH、无需 ffmpeg）。

已实测（2026-10-25）：
- 不带游客 cookie 直连 weibo.com/ajax/statuses/show → 302 跳游客墙；
- 网上大量旧教程用的 m.weibo.cn/statuses/show 现在直接返回 Sina Visitor System HTML，已失效；
- 唯一可用链路：genvisitor 拿 tid → incarnate 种 cookie → weibo.com/ajax/statuses/show。
"""

from __future__ import annotations

import json
import random
import re

from .. import http
from ..download import rank_of
from ..errors import AuthRequired, NoMedia, UpstreamChanged
from ..models import Asset, MediaInfo, Stream
from ..parsers import ext_of_url
from .base import Provider

_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_FP = {
    "os": "1",
    "browser": "Chrome141,0,0,0",
    "fonts": "undefined",
    "screenInfo": "1920*1080*24",
    "plugins": "",
}
_TEXT_RE = re.compile(r"<[^>]+>")
_MID_RE = re.compile(r"(?:status|detail|statuses/show\?id=)[/=]?(\d{6,})")
_WEB_ID_RE = re.compile(r"weibo\.com/\d+/([0-9A-Za-z]{9})")

_QUALITY_KEYS = (
    ("mp4_720p_mp4", "720P"),
    ("hevc_mp4_720p", "720P HEVC"),
    ("mp4_hd_url", "高清"),
    ("stream_url_hd", "高清"),
    ("h265_mp4_hd", "高清 HEVC"),
    ("mp4_sd_url", "标清"),
    ("stream_url", "标清"),
)


def bid2mid(bid: str) -> str:
    """weibo.com/{uid}/{bid} 的 base62 bid → 数字 mid。"""
    mid = ""
    for i in range(len(bid) - 4, -4, -4):
        chunk = bid[max(0, i): i + 4]
        value = 0
        for ch in chunk:
            value = value * 62 + _B62.index(ch)
        text = str(value)
        if i > 0:
            text = text.rjust(7, "0")
        mid = text + mid
    return mid


def mid_from_url(url: str) -> str | None:
    web = _WEB_ID_RE.search(url)
    if web:
        return bid2mid(web.group(1))
    numeric = _MID_RE.search(url)
    return numeric.group(1) if numeric else None


def _clean(text: str | None) -> str:
    return _TEXT_RE.sub("", text or "").strip()


class Weibo(Provider):
    name = "weibo"
    priority = 80

    def matches(self, url: str) -> bool:
        return "weibo.com" in url or "weibo.cn" in url

    async def resolve(self, url: str, *, quality: str = "best") -> MediaInfo:
        mid = mid_from_url(url)
        if not mid:
            raise NoMedia(
                "没识别出微博 mid",
                hint="请用 m.weibo.cn/status/<id> 或 weibo.com/<uid>/<bid> 链接",
                platform="weibo",
            )

        async with http.client("weibo", headers={"Referer": "https://weibo.com/"}) as cli:
            await self._visitor(cli)
            resp = await cli.get(
                "https://weibo.com/ajax/statuses/show",
                params={"id": mid},
                headers={"Referer": f"https://weibo.com/{mid}", "X-Requested-With": "XMLHttpRequest"},
            )
            text = resp.text
            if "visitor" in resp.url.path.lower() or text.lstrip().startswith("<"):
                raise AuthRequired(
                    "被微博游客墙拦截",
                    hint="导出浏览器 cookie（含 SUB）存到 ~/.config/vextract/cookies/weibo.txt 后重试",
                    platform="weibo",
                )
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise UpstreamChanged(
                    "微博接口返回的不是 JSON（可能已改版）",
                    hint="反馈该链接",
                    platform="weibo",
                    detail=text[:200],
                ) from exc

        media = ((data.get("page_info") or {}).get("media_info")) or {}
        streams = self._streams(media)
        assets = self._assets(data)
        if not streams and not assets:
            raise NoMedia(
                "该微博既没有视频也没有图片",
                hint="确认链接指向含媒体内容的微博（纯文字微博没有可下载的东西）",
                platform="weibo",
            )

        user = data.get("user") or {}
        stats = {
            k: int(data[src])
            for k, src in (("like", "attitudes_count"), ("comment", "comments_count"), ("repost", "reposts_count"))
            if isinstance(data.get(src), int)
        }
        return MediaInfo(
            platform="weibo",
            media_id=mid,
            source_url=f"https://m.weibo.cn/status/{mid}",
            title=_clean(data.get("text_raw") or data.get("text"))[:120] or f"weibo-{mid}",
            author=user.get("screen_name"),
            cover_url=media.get("cover_image") or media.get("cover_image_url") or (assets[0].url if assets else None),
            duration=self._duration(media),
            stats=stats,
            streams=streams,
            assets=assets,
            needs_merge=False,
            quality_note=None if streams else f"这条是图文微博（{len(assets)} 张图）：下载走图集模式",
            extra={"mid": mid, "uid": user.get("idstr")},
        )

    @staticmethod
    def _assets(data: dict) -> list[Asset]:
        """图文微博：`pic_ids` 给出顺序，`pic_infos[pid]` 给每个 pid 的各档链接。

        实测同一 pid 有 7 档（thumbnail 180x101 / bmiddle 360x202 / large 1706x960 /
        original 1920x1080 / largest 2048x1152 / mw2000 2000x1125 / largecover 1706x960）。
        **按 width×height 挑最大的一档**，不要按字段名猜"哪个是原图"。
        """
        infos = data.get("pic_infos") or {}
        if not isinstance(infos, dict):
            return []
        ordered = [str(pid) for pid in (data.get("pic_ids") or []) if str(pid) in infos] or list(infos)
        out: list[Asset] = []
        for index, pid in enumerate(ordered, start=1):
            entry = infos.get(pid) or {}
            variants = [
                value for value in entry.values()
                if isinstance(value, dict) and isinstance(value.get("url"), str)
            ]
            if not variants:
                continue
            best = max(variants, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
            url = str(best["url"])
            out.append(
                Asset(
                    url=url,
                    index=index,
                    ext=ext_of_url(url),
                    width=int(best["width"]) if isinstance(best.get("width"), int) else None,
                    height=int(best["height"]) if isinstance(best.get("height"), int) else None,
                    headers={"Referer": "https://weibo.com/"},
                )
            )
        return out

    @staticmethod
    def _duration(media: dict) -> float | None:
        """微博 duration 单位不稳定（有的毫秒、有的秒），>1000 视为毫秒。"""
        value = media.get("duration")
        if not isinstance(value, (int, float)):
            return None
        return float(value) / 1000 if value > 1000 else float(value)

    @staticmethod
    def _streams(media: dict) -> list[Stream]:
        out: list[Stream] = []
        seen: set[str] = set()
        playback = media.get("playback_list") or []
        ranked = sorted(
            playback,
            key=lambda p: (p.get("play_info") or {}).get("bitrate") or 0,
            reverse=True,
        )
        for entry in ranked:
            info = entry.get("play_info") or {}
            url = info.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            label = info.get("quality_desc") or info.get("label") or "hd"
            bitrate = int(info["bitrate"]) if isinstance(info.get("bitrate"), int) else 0
            out.append(
                Stream(
                    url=url,
                    quality=str(label),
                    ext="mp4",
                    codec=info.get("mime_type"),
                    bitrate=bitrate or None,
                    filesize=info.get("size"),
                    rank=rank_of(str(label), int(info.get("height") or 0), bitrate),
                    headers={"Referer": "https://weibo.com/"},
                )
            )
        for key, label in _QUALITY_KEYS:
            url = media.get(key)
            if url and url not in seen:
                seen.add(url)
                out.append(
                    Stream(
                        url=url,
                        quality=label,
                        ext="mp4",
                        rank=rank_of(label),
                        headers={"Referer": "https://weibo.com/"},
                    )
                )
        return sorted(out, key=lambda s: s.rank, reverse=True)

    @staticmethod
    async def _visitor(cli) -> None:
        """走新浪游客墙拿到 SUB 等 cookie（无需用户登录）。"""
        fp = json.dumps({**_FP, "timeZone": -8}, separators=(",", ":"))
        gen = await cli.get(
            "https://passport.weibo.com/visitor/genvisitor",
            params={"cb": "gen_callback", "fp": fp},
            headers={"Referer": "https://weibo.com/"},
        )
        tid = None
        match = re.search(r"gen_callback\((.*)\)", gen.text, re.S)
        if match:
            try:
                tid = json.loads(match.group(1)).get("data", {}).get("tid")
            except json.JSONDecodeError:
                tid = None
        if not tid:
            # 拿不到 tid 不算致命：可能已有历史 cookie，直接继续
            return
        await cli.get(
            "https://passport.weibo.com/visitor/visitor",
            params={
                "a": "incarnate",
                "t": tid,
                "w": 3,
                "c": 100,
                "cb": "cross_domain",
                "from": "weibo",
                "_rand": random.random(),
            },
            headers={"Referer": "https://weibo.com/"},
        )