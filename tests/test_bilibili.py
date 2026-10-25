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


# ---------- 字幕（登录态才有；实测 12 轨 / 2 轨 / 匿名 0 轨）----------
def test_subtitle_parse_from_player_v2_payload():
    """`x/player/wbi/v2` 的 subtitle.subtitles[] 结构（实测形状）。"""
    payload = {
        "code": 0,
        "data": {
            "need_login_subtitle": False,
            "subtitle": {
                "subtitles": [
                    {"lan": "zh-CN", "lan_doc": "中文（中国）", "subtitle_url": "//aisubtitle.hdslb.com/bfs/subtitle/a.json"},
                    {"lan": "ai-zh", "lan_doc": "中文", "subtitle_url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/b.json"},
                    {"lan": "en-US", "lan_doc": "English(US)", "subtitle_url": ""},
                ]
            },
        },
    }
    import asyncio

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    class FakeClient:
        """nav 请求要回 wbi 密钥，其余回字幕 payload——_subtitles 会先取密钥。"""

        async def get(self, url, **_k):
            if "/nav" in url:
                return FakeResponse(
                    {"code": 0, "data": {"wbi_img": {"img_url": "https://i0/x/" + "A" * 32 + ".png",
                                                     "sub_url": "https://i0/x/" + "B" * 32 + ".png"}}}
                )
            return FakeResponse(payload)

    provider = Bilibili()
    subs, need_login = asyncio.run(provider._subtitles(FakeClient(), 1, 2, "BV1"))
    assert need_login is False
    assert [s.lan for s in subs] == ["zh-CN", "ai-zh"], "空 subtitle_url 的轨要跳过"
    assert subs[0].url.startswith("https://"), "协议相对 URL 要补全"
    assert subs[1].ai is True and subs[0].ai is False
    assert "bilibili.com" in subs[0].headers.get("Referer", "")


def test_subtitle_gate_is_reported_not_raised():
    """匿名时字幕恒为空且 need_login_subtitle=true：这是事实，不是错误。"""
    import asyncio

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    class FakeClient:
        async def get(self, url, **_k):
            if "/nav" in url:
                return FakeResponse(
                    {"code": 0, "data": {"wbi_img": {"img_url": "https://i0/x/" + "A" * 32 + ".png",
                                                     "sub_url": "https://i0/x/" + "B" * 32 + ".png"}}}
                )
            return FakeResponse({"code": 0, "data": {"need_login_subtitle": True, "subtitle": {"subtitles": []}}})

    subs, need_login = asyncio.run(Bilibili()._subtitles(FakeClient(), 1, 2, "BV1"))
    assert subs == [] and need_login is True


# ---------- 多分P 合集（实测踩到：界面写 122:10，产物只有 9 分钟）----------
def multi_page_view() -> dict:
    return {
        "bvid": "BV1JE411N7UD",
        "cid": 166782869,
        "duration": 7330,          # 合集总时长（21 个分P）
        "pages": [
            {"cid": 166782869, "page": 1, "part": "开头", "duration": 564},
            {"cid": 166789445, "page": 2, "part": "序章1", "duration": 513},
            {"cid": 166837283, "page": 3, "part": "序章2", "duration": 408},
        ],
    }


def test_single_page_uses_view_duration():
    cid, duration, note = Bilibili._select_page({"cid": 137649199, "duration": 213, "pages": [{"cid": 137649199, "duration": 213}]}, 1)
    assert (cid, duration, note) == (137649199, 213.0, None), "单P 视频不该冒出合集提示"


def test_multi_page_uses_the_selected_page_duration_and_says_so():
    cid, duration, note = Bilibili._select_page(multi_page_view(), 1)
    assert cid == 166782869
    assert duration == 564.0, "duration 必须是这一分P 的时长，不是合集总时长"
    assert note and "3 个分P" in note and "第 1 分P" in note
    assert "2:02:10" in note, "提示里要说清合集总时长（7330s），用户才知道自己只下了一段"
    assert "?p=" in note, "要告诉用户怎么拿其它分P"


def test_page_param_selects_that_page():
    view = multi_page_view()
    cid, duration, _ = Bilibili._select_page(view, 2)
    assert cid == 166789445 and duration == 513.0


def test_page_param_out_of_range_falls_back_and_warns():
    cid, duration, note = Bilibili._select_page(multi_page_view(), 99)
    assert cid == 166837283 and duration == 408.0
    assert note and "超出范围" in note


def test_page_of_reads_query():
    from vextract.providers.bilibili import _page_of

    assert _page_of("https://www.bilibili.com/video/BV1JE411N7UD") == 1
    assert _page_of("https://www.bilibili.com/video/BV1JE411N7UD?p=3") == 3
    assert _page_of("https://www.bilibili.com/video/BV1JE411N7UD?spm=x&p=12") == 12


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