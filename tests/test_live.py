"""真机测试：默认跳过（pyproject 里 addopts = -m 'not live'）。

跑法：`uv run pytest -m live`
这些用例访问真实平台，用来发现"平台改版导致解析失效"——也就是本项目最大的风险。

抖音用例需要一个真实 UIFID：本机 ~/.config/vextract/cookies/douyin.txt 里已有就直接跑，
没有就 skip（不把"环境缺失"混成"平台坏了"）。
"""

from __future__ import annotations

import pytest

from vextract import cookies, registry

pytestmark = pytest.mark.live

BILI_URL = "https://www.bilibili.com/video/BV1GJ411x7h7/"
WEIBO_URL = "https://m.weibo.cn/detail/4189191225395228"
KS_URL = "https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa"
DY_URL = "https://www.douyin.com/video/6961737553342991651"


async def _extract(url: str):
    return await registry.extract(url)


@pytest.mark.asyncio
async def test_bilibili_live():
    media = await _extract(BILI_URL)
    assert media.platform == "bilibili"
    assert any(not s.audio_only for s in media.streams)
    assert media.needs_merge is True


@pytest.mark.asyncio
async def test_weibo_live():
    media = await _extract(WEIBO_URL)
    assert media.platform == "weibo"
    assert media.streams


@pytest.mark.asyncio
async def test_kuaishou_live():
    media = await _extract(KS_URL)
    assert media.platform == "kuaishou"
    assert any(s.codec == "hevc" for s in media.streams)


def _has_uifid() -> bool:
    path = cookies.cookie_file("douyin")
    if not path:
        return False
    names = {name for _domain, name, _value in cookies.load_netscape(path)}
    return bool(names & {"UIFID", "UIFID_TEMP", "uifid", "uifid_temp"})


@pytest.mark.asyncio
async def test_douyin_live_needs_only_a_browser_identity():
    """抖音：给了 UIFID 就该真的解析出来（自研签名 + 游客 ttwid）。

    这条用例的价值在于它会在平台改签名/改闸门时先红：一旦失败，
    要么重新抓 UIFID，要么说明 Argus 规则变了。
    """
    if not _has_uifid():
        pytest.skip("没有抖音 UIFID：先 `vx cookies import <导出文件> --platform douyin`")

    media = await _extract(DY_URL)
    assert media.platform == "douyin"
    assert media.media_id == "6961737553342991651"
    video = [s for s in media.streams if not s.audio_only]
    assert video, "应当至少有一路视频流"
    assert video[0].headers.get("Referer"), "抖音直链裸请求 403，必须带 Referer"
    assert media.duration and media.duration > 0