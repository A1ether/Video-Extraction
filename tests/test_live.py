"""真机测试：默认跳过（pyproject 里 addopts = -m 'not live'）。

跑法：`uv run pytest -m live`
这些用例访问真实平台，用来发现"平台改版导致解析失效"——也就是本项目最大的风险。
"""

from __future__ import annotations

import pytest

from vextract import registry

pytestmark = pytest.mark.live

BILI_URL = "https://www.bilibili.com/video/BV1GJ411x7h7/"
WEIBO_URL = "https://m.weibo.cn/detail/4189191225395228"
KS_URL = "https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa"


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


@pytest.mark.asyncio
async def test_douyin_without_cookie_raises_auth_required():
    """抖音无 cookie 必须给出 AUTH_REQUIRED + 可执行 hint，而不是原始报错。"""
    from vextract.errors import AuthRequired

    with pytest.raises(AuthRequired) as err:
        await _extract("https://www.douyin.com/video/6961737553342991651")
    assert "cookies-from-browser" in (err.value.hint or "")