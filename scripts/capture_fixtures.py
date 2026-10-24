#!/usr/bin/env python
"""抓真实响应存成 tests/fixtures（测试用，不参与运行时）。

用法：uv run python scripts/capture_fixtures.py
用途：平台改版时重跑一次，就能看到"解析失败"到底是代码问题还是平台变了。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx

from vextract import http
from vextract.providers.bilibili import sign_wbi

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

BILI_BVID = "BV1GJ411x7h7"
KS_ID = "3xwzr5dveyqc5fa"
WEIBO_MID = "4189191225395228"


async def capture_bilibili() -> None:
    async with http.client("bilibili", headers={"Referer": "https://www.bilibili.com/"}) as cli:
        view = await cli.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": BILI_BVID})
        nav = await cli.get("https://api.bilibili.com/x/web-interface/nav")
        (FIXTURES / "bilibili_view.json").write_text(view.text, encoding="utf-8")
        (FIXTURES / "bilibili_nav.json").write_text(nav.text, encoding="utf-8")

        img = nav.json()["data"]["wbi_img"]
        img_key = img["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub_key = img["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        cid = view.json()["data"]["cid"]
        params = sign_wbi(
            {"bvid": BILI_BVID, "cid": cid, "qn": 127, "fnver": 0, "fnval": 4048, "fourk": 1},
            img_key,
            sub_key,
        )
        play = await cli.get(
            "https://api.bilibili.com/x/player/wbi/playurl",
            params=params,
            headers={"Referer": "https://www.bilibili.com/"},
        )
        (FIXTURES / "bilibili_playurl.json").write_text(play.text, encoding="utf-8")
        print("bilibili: view/nav/playurl 已保存")


async def capture_weibo() -> None:
    async with http.client("weibo", headers={"Referer": "https://weibo.com/"}) as cli:
        from vextract.providers.weibo import Weibo

        await Weibo._visitor(cli)
        resp = await cli.get(
            "https://weibo.com/ajax/statuses/show",
            params={"id": WEIBO_MID},
            headers={"Referer": f"https://weibo.com/{WEIBO_MID}", "X-Requested-With": "XMLHttpRequest"},
        )
        (FIXTURES / "weibo_status.json").write_text(resp.text, encoding="utf-8")
        print(f"weibo: status json 已保存（{len(resp.text)} bytes）")


async def capture_kuaishou() -> None:
    async with http.client("kuaishou", mobile=True, headers={"Referer": "https://v.kuaishou.com/"}) as cli:
        resp = await cli.get(f"https://m.gifshow.com/fw/photo/{KS_ID}")
        (FIXTURES / "kuaishou_mobile.html").write_text(resp.text, encoding="utf-8")
        print(f"kuaishou: 移动页已保存（{len(resp.text)} bytes）")


async def capture_xhs(url: str | None) -> None:
    """小红书需要带 xsec_token 的真实分享链接；没有就跳过（不伪造数据）。"""
    if not url:
        readme = Path(__file__).resolve().parents[1] / "docs" / "research" / "raw" / "banana_readme.md"
        if readme.exists():
            found = re.findall(
                r"https://www\.xiaohongshu\.com/explore/\S+?xsec_token=\S+",
                readme.read_text(encoding="utf-8", errors="ignore"),
            )
            url = found[0].rstrip(")》,。") if found else None
    if not url:
        print("xhs: 跳过（需要人工提供带 xsec_token 的链接）")
        return
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=25,
        headers={"User-Agent": http.DESKTOP_UA, "Referer": "https://www.xiaohongshu.com/"},
    ) as cli:
        resp = await cli.get(url)
        (FIXTURES / "xhs_note_page.html").write_text(resp.text, encoding="utf-8")
        print(f"xhs: 笔记页已保存（{len(resp.text)} bytes，final={str(resp.url)[:80]}）")


async def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for job in (capture_bilibili(), capture_weibo(), capture_kuaishou(), capture_xhs(None)):
        try:
            await job
        except Exception as exc:  # noqa: BLE001
            print("抓取失败：", type(exc).__name__, exc)
    print("fixtures 目录：", FIXTURES)
    for path in sorted(FIXTURES.glob("*")):
        print(f"  {path.name:28} {path.stat().st_size:>9} bytes")


if __name__ == "__main__":
    asyncio.run(main())