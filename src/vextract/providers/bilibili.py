"""B站：自研 WBI 签名 + playurl（ADR-002）。

为什么不用现成库：bilibili-api（GPL-3.0）2026-07-06 收到律师函已 archived、issues 禁用；
Bili23-Downloader（GPL-3.0）与 PySide6 强耦合。WBI 签名纯逻辑只有 25 行，自研才保得住许可证。

实测（2026-10-25）：未登录请求 BV1GJ411x7h7 时 support_formats 声明到 1080P 高码率，
但 dash.video 实际只有 id=[16,32]（360P/480P）→ 未登录只能拿 480P，必须显式告知用户。
"""

from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import urlencode

from .. import http
from ..download import rank_of
from ..errors import AuthRequired, NoMedia, UpstreamChanged
from ..models import MediaInfo, Stream, Subtitle
from .base import Provider

_API = "https://api.bilibili.com"
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]
_QN_LABEL = {
    127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K", 116: "1080P60", 112: "1080P+",
    80: "1080P", 74: "720P60", 64: "720P", 32: "480P", 16: "360P",
}
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_PAGE_RE = re.compile(r"[?&]p=(\d+)")
_AV_RE = re.compile(r"/av(\d+)")

WEB_REFERER = {"Referer": "https://www.bilibili.com/"}


def _page_of(url: str) -> int:
    """链接里的 ?p=N（1-based）。没有就当第 1 个分P。"""
    match = _PAGE_RE.search(url)
    return max(1, int(match.group(1))) if match else 1


def _clock(seconds: float | int | None) -> str:
    total = int(seconds or 0)
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def mixin_key(img_key: str, sub_key: str) -> str:
    """WBI: 用 64 位重排表从 img_key+sub_key 取前 32 位。"""
    raw = img_key + sub_key
    if len(raw) < 64:
        raise UpstreamChanged(
            "WBI 密钥长度异常，B站可能已改签名方式",
            hint="先升级本项目；持续失败请反馈 `vx doctor` 输出",
            platform="bilibili",
        )
    return "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi(params: dict[str, object], img_key: str, sub_key: str, *, wts: int | None = None) -> dict[str, object]:
    """按 B站规则签名：过滤 !'()* → 排序 → urlencode → md5(query+mixin_key)。"""
    mixin = mixin_key(img_key, sub_key)
    signed = dict(params)
    signed["wts"] = int(time.time()) if wts is None else wts
    ordered = sorted((k, str(v)) for k, v in signed.items())
    cleaned = [(k, "".join(ch for ch in v if ch not in "!'()*")) for k, v in ordered]
    query = urlencode(cleaned)
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()  # noqa: S324 — 平台算法要求
    return signed


class Bilibili(Provider):
    name = "bilibili"
    priority = 90

    def matches(self, url: str) -> bool:
        return "bilibili.com" in url or "b23.tv" in url or "bili2233.cn" in url

    async def resolve(self, url: str, *, quality: str = "best") -> MediaInfo:
        bvid, aid = self._ids(url)
        if not bvid and not aid:
            raise NoMedia("没识别出 B站视频号（BV/av）", hint="请用视频页链接", platform="bilibili")

        async with http.client("bilibili", headers=WEB_REFERER) as cli:
            q = {"bvid": bvid} if bvid else {"aid": aid}
            view = http.check(await cli.get(f"{_API}/x/web-interface/view", params=q), "bilibili")
            payload = view.json()
            if payload.get("code") != 0:
                raise UpstreamChanged(
                    f"B站 view 接口返回 code={payload.get('code')}: {payload.get('message')}",
                    hint="链接可能已失效或被删除",
                    platform="bilibili",
                )
            data = payload["data"]
            cid, page_duration, page_note = self._select_page(data, _page_of(url))
            streams, note = await self._playurl(cli, data["bvid"], cid)
            subtitles, subtitle_gate = await self._subtitles(cli, data["aid"], cid, data["bvid"])

        videos = sorted(
            [s for s in streams if not s.audio_only],
            key=lambda s: (self._rnk(s.quality), s.bitrate or 0),
            reverse=True,
        )
        audios = sorted([s for s in streams if s.audio_only], key=lambda s: s.bitrate or 0, reverse=True)
        if not videos:
            raise NoMedia("B站未返回任何视频流", hint="可能是付费/充电专属内容", platform="bilibili")

        stat = data.get("stat") or {}
        return MediaInfo(
            platform="bilibili",
            media_id=data["bvid"],
            source_url=f"https://www.bilibili.com/video/{data['bvid']}",
            title=data.get("title") or "untitled",
            author=(data.get("owner") or {}).get("name"),
            cover_url=data.get("pic"),
            duration=page_duration,
            stats={
                k: int(stat[src])
                for k, src in (
                    ("view", "view"),
                    ("like", "like"),
                    ("comment", "reply"),
                    ("repost", "share"),
                    ("favorite", "favorite"),
                )
                if isinstance(stat.get(src), int)
            },
            streams=videos[:6] + audios[:3],
            subtitles=subtitles,
            needs_merge=bool(audios),
            quality_note="\n".join(part for part in (note, page_note) if part) or None,
            extra={
                "aid": data.get("aid"),
                "cid": cid,
                "bvid": data.get("bvid"),
                "pages": len(data.get("pages") or []),
                "page": _page_of(url),
                "need_login_subtitle": subtitle_gate,
            },
        )

    async def _subtitles(self, cli, aid: int, cid: int, bvid: str) -> tuple[list[Subtitle], bool]:
        """字幕轨：`x/player/wbi/v2` 的 `data.subtitle.subtitles[]`。

        实测（2026-10-25）：
        - **匿名恒为空**且 `need_login_subtitle=True`；
        - 带 SESSDATA（登录态）时真能拿到：`BV1GJ411x7h7` 有 12 轨
          （zh-CN / zh-Hans / zh-Hant / zh-HK / en-US / ja / ko / de-DE / ru / iw / ca / ase），
          `BV1JE411N7UD` 有 2 轨（人工 zh-CN + ai-zh）。
        所以这里不把"没有字幕"当错误，只把"要不要登录"作为事实回传（extra.need_login_subtitle）。
        """
        try:
            img_key, sub_key = await self._wbi_keys(cli)
            params = sign_wbi({"aid": aid, "cid": cid, "bvid": bvid}, img_key, sub_key)
            resp = await cli.get(f"{_API}/x/player/wbi/v2", params=params, headers=WEB_REFERER)
            body = resp.json()
        except Exception:  # noqa: BLE001 — 字幕是附加信息，拿不到不该让整条解析失败
            return [], False
        if body.get("code") != 0:
            return [], False
        data = body.get("data") or {}
        block = data.get("subtitle") or {}
        out: list[Subtitle] = []
        for item in block.get("subtitles") or []:
            url = item.get("subtitle_url") or ""
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            lan = str(item.get("lan") or "")
            out.append(
                Subtitle(
                    lan=lan,
                    lan_doc=str(item.get("lan_doc") or ""),
                    url=url,
                    ai=lan.startswith("ai-"),
                    headers={**WEB_REFERER, "User-Agent": http.DESKTOP_UA},
                )
            )
        return out, bool(data.get("need_login_subtitle"))

    @staticmethod
    def _select_page(data: dict, page_no: int) -> tuple[int, float | None, str | None]:
        """选集：多分P（合集）视频只解析一个 P。

        实测踩过：`BV1JE411N7UD` 是 21 个分P 的合集，`view.duration` 是**总时长**
        （7330s），而 `data["cid"]` 只是第 1 分P（564s）。旧实现拿总时长当 duration、
        只用第 1 分P 的流 → 界面写着 122:10、产物却只有 9 分钟，属于"静默给错东西"。
        现在：按链接里的 `?p=N` 选分P，duration 用**那一个分P**的时长，并把"合集"写进提示。
        """
        pages = [item for item in (data.get("pages") or []) if isinstance(item, dict)]
        if not pages:
            duration = data.get("duration")
            return int(data.get("cid") or 0), float(duration) if duration else None, None

        index = max(1, min(page_no, len(pages)))
        page = pages[index - 1]
        cid = int(page.get("cid") or data.get("cid") or 0)
        duration = page.get("duration") or data.get("duration")
        note = None
        if len(pages) > 1:
            total = data.get("duration")
            note = (
                f"这是 {len(pages)} 个分P 的合集"
                + (f"（总时长 {_clock(total)}）" if total else "")
                + f"，本次只处理第 {index} 分P（{page.get('part') or ''}"
                + (f" · {_clock(duration)}" if duration else "")
                + "）；要其它分P 请在链接后加 ?p=N"
            )
            if page_no > len(pages):
                note = f"链接里的 p={page_no} 超出范围（共 {len(pages)} 个分P），已回退到最后一个分P。" + note
        return cid, float(duration) if duration else None, note

    # --- 内部 ---
    @staticmethod
    def _rnk(label: str) -> int:
        for qn, text in _QN_LABEL.items():
            if label.startswith(text):
                return qn
        return 0

    @staticmethod
    def _ids(url: str) -> tuple[str | None, str | None]:
        bv = _BV_RE.search(url)
        if bv:
            return bv.group(0), None
        av = _AV_RE.search(url)
        if av:
            return None, av.group(1)
        return None, None

    async def _wbi_keys(self, cli) -> tuple[str, str]:
        nav = await cli.get(f"{_API}/x/web-interface/nav", headers=WEB_REFERER)
        img = (nav.json().get("data") or {}).get("wbi_img") or {}
        img_url, sub_url = img.get("img_url") or "", img.get("sub_url") or ""
        if not img_url or not sub_url:
            raise UpstreamChanged(
                "没能从 nav 接口取到 WBI 密钥",
                hint="B站匿名接口偶发限流，稍后重试；持续失败请反馈",
                platform="bilibili",
            )
        return img_url.rsplit("/", 1)[-1].split(".")[0], sub_url.rsplit("/", 1)[-1].split(".")[0]

    async def _playurl(self, cli, bvid: str, cid: int) -> tuple[list[Stream], str | None]:
        img_key, sub_key = await self._wbi_keys(cli)
        params = sign_wbi(
            {"bvid": bvid, "cid": cid, "qn": 127, "fnver": 0, "fnval": 4048, "fourk": 1},
            img_key,
            sub_key,
        )
        resp = http.check(await cli.get(f"{_API}/x/player/wbi/playurl", params=params, headers=WEB_REFERER), "bilibili")
        body = resp.json()
        if body.get("code") != 0:
            raise AuthRequired(
                f"B站 playurl 返回 code={body.get('code')}: {body.get('message')}",
                hint="该视频可能需要登录（SESSDATA）才能取流",
                platform="bilibili",
            )
        data = body.get("data") or {}
        dash = data.get("dash") or {}
        out: list[Stream] = []

        for item in dash.get("video") or []:
            qn = int(item.get("id") or 0)
            base = item.get("baseUrl") or item.get("base_url")
            if not base:
                continue
            codec = {7: "h264", 12: "hevc", 13: "av1"}.get(item.get("codecid"))
            label = _QN_LABEL.get(qn, f"{qn}")
            bandwidth = int(item.get("bandwidth") or 0)
            out.append(
                Stream(
                    url=base,
                    quality=label,
                    ext="mp4",
                    codec=codec,
                    bitrate=bandwidth,
                    rank=rank_of(label, int(item.get("height") or 0), bandwidth),
                    headers={**WEB_REFERER, "User-Agent": http.DESKTOP_UA},
                )
            )
        for item in dash.get("audio") or []:
            base = item.get("baseUrl") or item.get("base_url")
            if not base:
                continue
            bandwidth = int(item.get("bandwidth") or 0)
            label = f"audio-{bandwidth // 1000}k"
            out.append(
                Stream(
                    url=base,
                    quality=label,
                    ext="m4a",
                    codec="aac",
                    audio_only=True,
                    bitrate=bandwidth,
                    rank=rank_of("audio", bitrate=bandwidth),
                    headers={**WEB_REFERER, "User-Agent": http.DESKTOP_UA},
                )
            )
        if not dash:
            for item in data.get("durl") or []:
                if item.get("url"):
                    out.append(
                        Stream(
                            url=item["url"],
                            quality="flv",
                            ext="flv",
                            rank=rank_of("flv"),
                            headers=WEB_REFERER,
                        )
                    )

        note = None
        declared = [f"qn={q}" for q in (data.get("accept_quality") or [])]
        max_qn = max((int(i.get("id") or 0) for i in (dash.get("video") or [])), default=0)
        if max_qn and max_qn <= 32:
            note = (
                f"实取最高 {_QN_LABEL.get(max_qn, max_qn)}"
                + (f"（接口声明可用：{', '.join(declared)}）" if declared else "")
                + "；未登录只能拿 480P 及以下，1080P+ 需提供 SESSDATA cookie"
            )
        return out, note