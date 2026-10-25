"""抖音：自研 provider（详情接口 + 自算签名），不再依赖 yt-dlp / a_bogus。

## 实测结论（2026-10-25，本机，证据见 docs/01 §4.3、§4.4）

风控（Argus）在 `aweme/v1/web/aweme/detail/` 上有**两道**闸，逐项拆开验证过：

| 组件 | 是否必需 | 来源 |
|---|---|---|
| cookie `UIFID`（320 字符） | **必需** | 只能由真实浏览器生成；本地任何入口都不下发（首页/详情/分享页都试过） |
| 查询参数 `uifid` | **必需** | 内容必须与 cookie 中的值一致 |
| cookie `ttwid` | **必需** | 可自造：`ttwid.bytedance.com/ttwid/union/register/` 免费下发游客 ttwid |
| `timestamp`（秒） | **必需** | 参与签名 |
| `x-secsdk-web-signature` | **必需** | `md5(f"{uifid}_{ts}_{SALT}_{query}")`，纯函数，无状态 |
| `a_bogus` | **不需要** | 浏览器会带，但去掉照样 200（实测删掉仍成功） |
| `msToken` | **不需要** | 同上 |
| `verifyFp` / `fp` | **不需要** | 同上 |
| Referer | **必需** | 缺它返回空 body（200 但 0 字节，容易被误判成"没数据"） |

关键发现：`a_bogus`（那些需要 849 行 VM 逆向代码的签名）在这个接口上**根本不是必需项**。
Argus 真正校验的是那个 32 位 md5 串，而它是 UIFID + 时间戳 + 固定 salt + query 的纯函数。
**该实现与抖音自己浏览器请求的签名逐字节一致**（用真实浏览器当 oracle 核对过，见 docs/01 §4.4）。

## 为什么还留 yt-dlp 兜底

用户完全没有 cookie 时，provider 抛 AUTH_REQUIRED 并给出"怎么拿 UIFID"的一步指引；
如果拿到 cookie 后详情接口仍不可用，仍可让用户手动 `--cookies-from-browser` 走 yt-dlp 老路。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .. import config, cookies
from ..download import rank_of
from ..errors import AuthRequired, NoMedia, RateLimited, UpstreamChanged
from ..models import Asset, MediaInfo, Stream
from ..parsers import ext_of_url
from .base import Provider

# Argus / secsdk 的固定 salt（douyin_web 项目的 project-id=34）。
# 它与浏览器无关、与账号无关，只是那套 SDK 的常量；换项目（如别的字节站点）才会变。
SIGNATURE_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"

DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
TTWID_REGISTER = "https://ttwid.bytedance.com/ttwid/union/register/"
REFERER = "https://www.douyin.com/"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# 作品 ID 的三种来源：标准页 / 短链展开后的 share 页 / 手机端 modal_id
_AWEME_ID_RES = (
    re.compile(r"/(?:video|note)/(\d{15,25})"),
    re.compile(r"[?&](?:modal_id|aweme_id|item_ids)=(\d{15,25})"),
    re.compile(r"/(\d{15,25})(?:[/?#]|$)"),
)

# 游客 ttwid 的注册体：这段是公开的、不含任何凭据（字节统一的 ttwid 服务中心）
_TTWID_PAYLOAD = json.dumps(
    {
        "region": "cn",
        "aid": 1768,
        "needFid": False,
        "service": "www.ixigua.com",
        "migrate_info": {"ticket": "", "source": "node"},
        "cbUrlProtocol": "https",
        "union": True,
    }
)

def _ttwid_cache_path() -> Path:
    """缓存放用户配置目录（与其它状态一致），不散落到 /tmp。"""
    return config.settings.home / "ttwid-cache"


def aweme_id_of(url: str) -> str | None:
    for pattern in _AWEME_ID_RES:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def sign_query(query: str, uifid: str, timestamp: int) -> str:
    """Argus 校验的那个 32 位 md5。纯函数：同样输入永远同样输出（无 nonce）。"""
    return hashlib.md5(f"{uifid}_{timestamp}_{SIGNATURE_SALT}_{query}".encode()).hexdigest()


def encode_pairs(pairs: list[tuple[str, str]]) -> str:
    """按 URLSearchParams.toString() 的规则拼接（*-._ 不转义），签名与发出去必须是同一串。"""
    return "&".join(f"{quote(k, safe='*-._')}={quote(v, safe='*-._')}" for k, v in pairs)


class Douyin(Provider):
    name = "douyin"
    priority = 88

    def matches(self, url: str) -> bool:
        return "douyin.com" in url or "iesdouyin.com" in url

    async def resolve(self, url: str, *, quality: str = "best") -> MediaInfo:
        aweme_id = aweme_id_of(url)
        if not aweme_id:
            raise NoMedia(
                "没识别出抖音作品 ID",
                hint="请用 https://www.douyin.com/video/<id> 或分享短链（v.douyin.com/...）",
                platform="douyin",
            )

        uifid = self._uifid()
        if not uifid:
            raise AuthRequired(
                "抖音需要一次浏览器身份（UIFID），本地无法凭空生成",
                hint=(
                    "L1：用 Cookie-Editor 之类插件导出抖音 cookie，"
                    "再 `vx cookies import <导出文件> --platform douyin`（只需 UIFID 那一类字段）\n"
                    "L2：或 `vx get <链接> --cookies-from-browser chrome` 让 yt-dlp 直接读浏览器"
                ),
                platform="douyin",
            )

        ttwid = await self._ttwid()
        detail = await self._fetch_detail(aweme_id, uifid, ttwid)
        return self._to_media(detail, aweme_id)

    # --- 身份 ---
    @staticmethod
    def _uifid() -> str | None:
        """UIFID 的三个来源，按"不打扰用户"排序：L1 文件 → L2 浏览器（只读）→ 无。

        UIFID 是浏览器的持久访客标识，服务端不下发、也无法本地凭空生成（实测：
        首页/详情/分享页/hot list 都不 Set-Cookie 它），所以"没有就老实要一次"。
        """
        path = cookies.cookie_file("douyin")
        if path:
            table = {name: value for _domain, name, value in cookies.load_netscape(path)}
            for key in ("UIFID", "UIFID_TEMP", "uifid", "uifid_temp"):
                if table.get(key):
                    return table[key]
        return _uifid_from_browser(config.settings.cookies_from_browser)

    async def _ttwid(self) -> str:
        """游客 ttwid：免费注册，可缓存复用。失败不致命（有时不校验），但不能静默当成功。"""
        path = _ttwid_cache_path()
        cached = _read_cache(path)
        if cached:
            return cached
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                resp = await cli.post(TTWID_REGISTER, content=_TTWID_PAYLOAD, headers={"Content-Type": "text/plain"})
            token = resp.cookies.get("ttwid")
        except httpx.HTTPError:
            token = None
        if token:
            _write_cache(path, token)
            return token
        raise UpstreamChanged(
            "拿不到游客 ttwid（抖音的身份注册接口不可用）",
            hint="稍后重试；若持续失败，说明该接口已变更，需要更新 provider",
            platform="douyin",
        )

    # --- 取数据 ---
    async def _fetch_detail(self, aweme_id: str, uifid: str, ttwid: str) -> dict:
        pairs: list[tuple[str, str]] = [
            ("device_platform", "webapp"),
            ("aid", "6383"),
            ("aweme_id", aweme_id),
        ]
        timestamp = int(time.time())
        covered = pairs + [("uifid", uifid), ("timestamp", str(timestamp))]
        query = encode_pairs(covered)
        signature = sign_query(query, uifid, timestamp)

        jar = httpx.Cookies()
        jar.set("UIFID", uifid, domain=".douyin.com")
        jar.set("ttwid", ttwid, domain=".douyin.com")
        headers = {
            "User-Agent": UA,
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
        }

        async with httpx.AsyncClient(cookies=jar, headers=headers, timeout=20, follow_redirects=True) as cli:
            resp = await cli.get(f"{DETAIL_API}?{query}&x-secsdk-web-signature={signature}")

        if resp.status_code in (403, 429):
            body = resp.text[:100]
            if "ArgusSecurityPlugin" in body:
                raise RateLimited(
                    "抖音 Argus 拒绝了这次请求（身份或签名不被认可）",
                    hint=(
                        "多为 UIFID 失效：重新导出一次抖音 cookie 再 `vx cookies import`；\n"
                        "若换了网络出口也可能触发，稍后重试"
                    ),
                    platform="douyin",
                    detail=body,
                )
            raise RateLimited(f"抖音返回 {resp.status_code}", hint="稍后重试或换网络出口", platform="douyin")
        if not resp.text.strip():
            # 200 但空 body：实测就是 Referer 缺失/被剥离的典型表现，不要误报成"作品不存在"
            raise UpstreamChanged(
                "抖音返回空响应（200 但 0 字节）",
                hint="通常是 Referer 被中间层剥掉或出口被风控；换网络/重试一次",
                platform="douyin",
            )
        try:
            payload = json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise UpstreamChanged(
                "抖音返回的不是 JSON（接口可能已改版）",
                hint="反馈该链接（附 --debug）",
                platform="douyin",
                detail=str(exc),
            ) from exc

        detail = payload.get("aweme_detail")
        if not detail:
            reason = (payload.get("filter_detail") or {}).get("detail_msg") or ""
            raise NoMedia(
                "抖音没有返回这条作品" + (f"：{reason}" if reason else ""),
                hint="确认作品在浏览器里能正常播放（可能已删除、仅粉丝可见或需登录）",
                platform="douyin",
                detail=str(payload.get("filter_detail") or "")[:200],
            )
        return detail

    # --- 归一化 ---
    def _to_media(self, detail: dict, aweme_id: str) -> MediaInfo:
        video = detail.get("video") or {}
        streams = self._streams(video)
        assets = self._assets(detail)
        if not streams and not assets:
            raise NoMedia(
                "作品既没有视频也没有图片",
                hint="确认链接指向视频/图文作品；若浏览器能播，说明接口字段变了，请反馈",
                platform="douyin",
            )

        author = (detail.get("author") or {}).get("nickname")
        stats_raw = detail.get("statistics") or {}
        stats = {
            key: int(stats_raw[src])
            for key, src in (
                ("like", "digg_count"),
                ("comment", "comment_count"),
                ("repost", "share_count"),
                ("collect", "collect_count"),
                ("view", "play_count"),
            )
            if isinstance(stats_raw.get(src), int) and int(stats_raw[src]) > 0
        }
        duration_ms = video.get("duration") or detail.get("duration")

        return MediaInfo(
            platform="douyin",
            media_id=aweme_id,
            source_url=f"https://www.douyin.com/video/{aweme_id}",
            title=str(detail.get("desc") or f"douyin-{aweme_id}"),
            author=author,
            cover_url=_first_url(video.get("cover") or video.get("origin_cover") or {}),
            duration=float(duration_ms) / 1000 if isinstance(duration_ms, (int, float)) and duration_ms > 0 else None,
            stats=stats,
            streams=streams,
            assets=assets,
            needs_merge=False,  # 抖音给的是音视频一体的 mp4
            quality_note=None if streams else f"这条是图文作品（{len(assets)} 张图）：下载走图集模式",
            extra={"aweme_type": detail.get("aweme_type"), "is_h265": video.get("is_h265")},
        )

    @staticmethod
    def _assets(detail: dict) -> list[Asset]:
        """抖音图文：`images[]` 每项与 `video.cover` 是同一套 url_list 结构。

        实测口径：本机能拿到的样本作品是视频（`images`/`image_list` 都为空），
        图文作品的字段形状按同一套 url_list 约定实现（`url_list[0]` 为直链）。
        线上正例待一条未失效的图文链接来定案，见 docs/03。
        """
        entries = detail.get("images") or detail.get("image_list") or []
        out: list[Asset] = []
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            urls = [item for item in (entry.get("url_list") or []) if isinstance(item, str) and item]
            if not urls and isinstance(entry.get("url"), str):
                urls = [entry["url"]]
            if not urls:
                continue
            width, height = entry.get("width"), entry.get("height")
            out.append(
                Asset(
                    url=urls[0],
                    index=index,
                    ext=ext_of_url(urls[0]),
                    width=int(width) if isinstance(width, int) else None,
                    height=int(height) if isinstance(height, int) else None,
                    headers={"Referer": REFERER},
                )
            )
        return out

    @staticmethod
    def _streams(video: dict) -> list[Stream]:
        headers = {"Referer": REFERER}
        out: list[Stream] = []

        # bit_rate 是"转码档位"清单：每档自带 play_addr，是选清晰度的主通道。
        # 注意 gear_name 里的数字才是抖音标的档位（normal_1080_0），而 addr.height 是长边
        # ——竖屏 1080x1920 时 height=1920，直接拿来当标签会把 1080p 说成 1920p。
        for rate in video.get("bit_rate") or []:
            if not isinstance(rate, dict):
                continue
            addr = rate.get("play_addr") or {}
            urls = addr.get("url_list") or []
            if not urls:
                continue
            side = _short_side(addr) or _short_side(video)
            bitrate = rate.get("bit_rate")
            label = _gear_label(rate.get("gear_name"), side)
            out.append(
                Stream(
                    url=urls[0],
                    quality=label,
                    codec="h265" if rate.get("is_h265") or rate.get("is_bytevc1") else "h264",
                    ext="mp4",
                    bitrate=int(bitrate) if isinstance(bitrate, (int, float)) else None,
                    filesize=addr.get("data_size"),
                    rank=rank_of(label, side, int(bitrate or 0)),
                    headers=headers,
                )
            )

        # play_addr 是"默认档"，常常就是最高档的同一份文件；去重时按 uri/size 认，不按 URL 认
        # （同一文件的 CDN 节点不同，URL 一定不同）。
        default_urls = (video.get("play_addr") or {}).get("url_list") or []
        if default_urls:
            addr = video.get("play_addr") or {}
            side = _short_side(addr) or _short_side(video)
            out.append(
                Stream(
                    url=default_urls[0],
                    quality=f"{side}p 原片" if side else "原片",
                    codec="h265" if video.get("is_h265") else "h264",
                    ext="mp4",
                    filesize=addr.get("data_size"),
                    rank=rank_of("origin", side),
                    headers=headers,
                )
            )

        seen_keys: set[object] = set()
        unique: list[Stream] = []
        for stream in sorted(out, key=lambda s: s.rank, reverse=True):
            key = stream.filesize or stream.url  # 同尺寸 = 同一份文件
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unique.append(stream)
        return unique[:6]


def _gear_label(gear: str | None, side: int) -> str:
    """`normal_1080_0` → `1080p`；`lower_540_0` → `540p 低码率`。

    档位数字优先取 gear_name（抖音自己标的档），没有再退回短边像素。
    """
    name = (gear or "").strip()
    label = f"{side}p" if side else ""
    if name:
        parts = name.split("_")
        if len(parts) >= 2 and parts[1].isdigit():
            label = f"{parts[1]}p"
        elif not label:
            label = name
    if name.startswith("lower") and label:
        label += " 低码率"
    return label or "video"


def _short_side(node: dict) -> int:
    """分辨率按短边算（竖屏 1080x1920 的档位是 1080，不是 1920）。"""
    width = int(node.get("width") or 0)
    height = int(node.get("height") or 0)
    sides = [side for side in (width, height) if side > 0]
    return min(sides) if sides else 0


def _first_url(node: dict) -> str | None:
    urls = node.get("url_list") if isinstance(node, dict) else None
    return urls[0] if urls else None


def _uifid_from_browser(browser: str | None) -> str | None:
    """L2：从用户本机浏览器只读地取 UIFID（不写、不打印、不落盘）。

    只取这一个字段，不把整罐 cookie 搬过来：抖音这条路只需要身份 ID，
    其余 cookie 一律不碰，权限与隐私面都更小。读不到（没装/锁库/无权限）就返回 None。
    """
    if not browser:
        return None
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        return None
    try:
        jar = extract_cookies_from_browser(browser)
    except Exception:  # noqa: BLE001 — 浏览器未装/数据库被锁/钥匙串未授权，都当作"没有"
        return None
    for cookie in jar:
        if cookie.name in ("UIFID", "UIFID_TEMP", "uifid", "uifid_temp"):
            return cookie.value
    return None


def _read_cache(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token:
        return None
    # ttwid 有效期约 1 年，但注册令牌换得勤；超过 3 天就重注册，避免把过期值一直用下去。
    try:
        if time.time() - path.stat().st_mtime > 3 * 24 * 3600:
            return None
    except OSError:
        return None
    return token


def _write_cache(path: Path, token: str) -> None:
    # 缓存写不进去不影响本次请求（只是下次要重新注册），所以不向上抛。
    # 目录可能是新建的，所以 mkdir 也在保护范围内。
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        os.chmod(path, 0o600)


__all__ = ["Douyin", "aweme_id_of", "encode_pairs", "sign_query", "SIGNATURE_SALT"]