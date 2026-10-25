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


DY_URL = "https://www.douyin.com/video/6961737553342991651"


@pytest.mark.asyncio
async def test_douyin_either_works_or_explains_the_argus_gate():
    """抖音当前必被 Argus 挡：要么解析成功（路线改好后），要么 AUTH_REQUIRED 且说清缺什么。

    注意别把这条写成"必须有 cookie"——实测带 70 条登录 cookie 也一样被拒（docs/01 §4.3）。
    """
    from vextract.errors import AuthRequired

    try:
        media = await _extract(DY_URL)
    except AuthRequired as err:
        hint = err.hint or ""
        assert ("vx cookies import" in hint) or ("a_bogus" in hint)  # 必须给出可执行的下一步
        assert "Traceback" not in hint
        return
    assert media.platform == "douyin"
    assert any(not s.audio_only for s in media.streams)