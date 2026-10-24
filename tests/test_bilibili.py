"""B站：WBI 签名（黄金向量锁定算法形状）+ 真实 playurl 响应解析。

黄金向量来自对 B站真实接口的成功调用（同一 sign_wbi 实现实测取流成功），
所以它锁的是"算法没被改坏"，不是"某个读数"。
"""

from __future__ import annotations

import json

import pytest

from vextract.errors import UpstreamChanged
from vextract.providers.bilibili import Bilibili, mixin_key, sign_wbi


def test_mixin_key_takes_first_32_after_reorder():
    assert mixin_key("A" * 32, "B" * 32) == "BBAABAABABAABABBABABBABAAAABABBA"


def test_mixin_key_rejects_short_input():
    with pytest.raises(UpstreamChanged):
        mixin_key("A" * 10, "B" * 10)


def test_sign_wbi_golden():
    signed = sign_wbi({"bvid": "BV1xx411c7mD", "cid": 123}, "A" * 32, "B" * 32, wts=1700000000)
    assert signed["w_rid"] == "cc393da093ec50b1f8a29cf687a640d6"
    assert signed["wts"] == 1700000000


def test_sign_wbi_filters_forbidden_chars():
    a = sign_wbi({"k": "a!b'c(d)e*f"}, "A" * 32, "B" * 32, wts=1)
    b = sign_wbi({"k": "abcdef"}, "A" * 32, "B" * 32, wts=1)
    assert a["w_rid"] == b["w_rid"]


@pytest.mark.parametrize(
    ("url", "bvid", "aid"),
    [
        ("https://www.bilibili.com/video/BV1GJ411x7h7/?spm_id_from=x", "BV1GJ411x7h7", None),
        ("https://www.bilibili.com/video/av170001", None, "170001"),
        ("https://b23.tv/bad", None, None),
    ],
)
def test_id_extraction(url, bvid, aid):
    assert Bilibili._ids(url) == (bvid, aid)


def test_parse_real_playurl_fixture(fixture):
    body = json.loads(fixture("bilibili_playurl.json"))
    assert body["code"] == 0
    dash = body["data"]["dash"]
    videos = [item for item in dash["video"]]
    audios = [item for item in dash["audio"]]
    assert videos and audios, "fixture 必须同时含视频轨与音频轨（DASH 分离）"
    # 匿名访问实测：接口声明到 1080P+，实际只给 480P 及以下
    assert max(int(item["id"]) for item in videos) <= 32
    assert all(item.get("baseUrl") or item.get("base_url") for item in videos)


def test_streams_from_real_playurl_are_ranked(fixture, monkeypatch):
    import asyncio

    playurl = json.loads(fixture("bilibili_playurl.json"))
    view = json.loads(fixture("bilibili_view.json"))
    nav = json.loads(fixture("bilibili_nav.json"))

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            if "view" in url:
                return FakeResp(view)
            if "nav" in url:
                return FakeResp(nav)
            return FakeResp(playurl)

    monkeypatch.setattr("vextract.providers.bilibili.http.client", lambda *a, **k: FakeClient())
    media = asyncio.run(Bilibili().resolve("https://www.bilibili.com/video/BV1GJ411x7h7/"))

    assert media.platform == "bilibili"
    assert media.needs_merge is True
    assert media.streams[0].audio_only is False
    assert media.streams[0].rank >= media.streams[1].rank
    assert any(s.audio_only for s in media.streams)
    assert media.quality_note and "未登录" in media.quality_note
    assert media.stats.get("view", 0) > 0