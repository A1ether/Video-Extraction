"""拓展能力探针：查证「音频 / 弹幕 / 字幕 / 图文」这些上游到底给不给数据。

结论写进 docs（`docs/03-拓展能力实测.md`），本脚本用来复现——口径与项目一致：
**不采信上游 README 自述，只认本机实测**。

    uv run python scripts/probe_extras.py

只读不写，不下载任何媒体文件。会访问真实平台（抖音需已配置 UIFID cookie）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vextract import http  # noqa: E402
from vextract.providers.bilibili import sign_wbi  # noqa: E402
from vextract.providers.douyin import Douyin  # noqa: E402

REF = {"Referer": "https://www.bilibili.com/"}
# 抖音：docs 里用过的样本作品（有 cookie 时能复现）
DY_SAMPLE = "6961737553342991651"
# B站：前两条是"声称有 CC 字幕"的视频（第三方 CC 工具点名过），第三条是普通 MV
BV_SAMPLES = ("BV1JE411N7UD", "BV1Sb41127UZ", "BV1GJ411x7h7")
# 只在键名里找这些词，判断上游有没有"文本轨"类字段
TEXT_TOKENS = ("subtitle", "caption", "danmaku", "danmu", "asr", "ocr", "transcript")


def _key_hits(obj: object, path: str = "", depth: int = 0) -> list[str]:
    """递归列出疑似字幕/文本轨字段（只看键名与前 160 字，避免刷屏与泄隐私）。"""
    if depth > 4:
        return []
    out: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else key
            if any(tok in key.lower() for tok in TEXT_TOKENS):
                out.append(f"{here} = {str(value)[:160]}")
            out += _key_hits(value, here, depth + 1)
    elif isinstance(obj, list):
        for i, value in enumerate(obj[:2]):
            out += _key_hits(value, f"{path}[{i}]", depth + 1)
    return out


def probe_ffmpeg() -> None:
    print("=" * 72)
    print("① ffmpeg 是否存在 + 是否有转音频所需的编码器（决定「仅音频」能做到什么格式）")
    path = shutil.which("ffmpeg")
    print("ffmpeg:", path or "未找到（音频转码与弹幕内嵌都做不了）")
    if not path:
        return
    out = subprocess.run(  # noqa: S603
        [path, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20
    ).stdout
    for name in ("libmp3lame", "aac", "flac", "libopus", "libvorbis"):
        found = [ln for ln in out.splitlines() if f" {name} " in ln]
        print(f"  {name}:", "✅" if found else "❌")


async def probe_douyin() -> None:
    print("=" * 72)
    print("② 抖音详情接口：有没有字幕轨 / 配音轨 / 图文（图文=可下载的图片集）")
    provider = Douyin()
    uifid = provider._uifid()
    if not uifid:
        print("未配置 UIFID cookie，跳过（先 `vx cookies import ... --platform douyin`）")
        return
    ttwid = await provider._ttwid()
    detail = await provider._fetch_detail(DY_SAMPLE, uifid, ttwid)
    print(f"顶层字段数: {len(detail)}")
    video = detail.get("video") or {}
    print("video 字段:", sorted(video.keys()))
    print("音频相关字段:", [k for k in video if "audio" in k or "music" in k])
    hits = _key_hits(detail, "detail")
    print(f"疑似文本轨字段（{len(hits)} 个）:")
    for line in hits:
        print("   ", line)
    print("images（图文图片集）条数:", len(detail.get("images") or []))
    print("image_list 条数:", len(detail.get("image_list") or []))


async def _bili(cli, img_key: str, sub_key: str, bvid: str) -> None:
    view = (await cli.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})).json()
    if view.get("code") != 0:
        print(f"{bvid}: view 失败 code={view.get('code')} {view.get('message')}")
        return
    data = view["data"]
    cid = data["cid"]
    params = sign_wbi({"aid": data["aid"], "cid": cid, "bvid": bvid}, img_key, sub_key)
    player = (await cli.get("https://api.bilibili.com/x/player/wbi/v2", params=params, headers=REF)).json()
    pdata = player.get("data") or {}
    subtitle = pdata.get("subtitle") or {}
    subs = subtitle.get("subtitles") or []
    print(
        f"{bvid} 《{(data.get('title') or '')[:24]}》"
        f" CC字幕={len(subs)} need_login_subtitle={pdata.get('need_login_subtitle')}"
        f" asr_language={pdata.get('asr_language')!r}"
    )
    for item in subs:
        url = item.get("subtitle_url") or ""
        print(f"    → {item.get('lan')}/{item.get('lan_doc')} {url[:70]}")
        if url:
            url = "https:" + url if url.startswith("//") else url
            body = (await cli.get(url)).json()
            lines = body.get("body") or []
            head = json.dumps(lines[0], ensure_ascii=False)[:110] if lines else "-"
            print(f"      字幕条数 {len(lines)}，首条 {head}")

    # 弹幕：list.so 是 deflate 压缩的 XML，匿名可取（无需登录）
    resp = await cli.get(f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}")
    try:
        xml = zlib.decompressobj(-zlib.MAX_WBITS).decompress(resp.content).decode("utf-8", "ignore")
    except zlib.error:
        xml = resp.content.decode("utf-8", "ignore")
    print(f"    弹幕 XML {len(resp.content)} 字节，<d> 条数 {xml.count('<d p=')}")


async def probe_bilibili() -> None:
    print("=" * 72)
    print("③ B站：CC 字幕是否需要登录 + 弹幕是否匿名可取")
    async with http.client("bilibili", headers=REF) as cli:
        nav = (await cli.get("https://api.bilibili.com/x/web-interface/nav")).json()
        img = (nav.get("data") or {}).get("wbi_img") or {}
        if not img:
            print("拿不到 WBI 密钥，跳过")
            return
        img_key = img["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub_key = img["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        for bvid in BV_SAMPLES:
            await _bili(cli, img_key, sub_key, bvid)


async def main() -> None:
    probe_ffmpeg()
    for step in (probe_douyin, probe_bilibili):
        try:
            await step()
        except Exception as exc:  # noqa: BLE001 — 探针失败不中断其余项
            print(f"{step.__name__} 失败: {type(exc).__name__} {exc}")


asyncio.run(main())