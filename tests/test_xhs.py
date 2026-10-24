"""小红书：零签名 SSR 路线。

这里有两类 fixture：
- `xhs_note_page.html` 是**真实抓取**的页面（该链接已失效 → 302 到 404 页），用来断言
  "笔记不可用时我们是干净地报 NO_MEDIA，而不是抛解析异常"。
- 视频分支的 fixture 是**按真实字段结构手工构造**的（本机没有可用的带 xsec_token 的
  视频分享链接）。它验证解析逻辑，不代表线上已验证——线上正例需要用户提供分享链接。
"""

from __future__ import annotations

import json

import pytest

from vextract.errors import NoMedia, UpstreamChanged
from vextract.parsers import extract_js_object
from vextract.providers.xhs import Xiaohongshu

SYNTHETIC_VIDEO_STATE = {
    "note": {
        "noteDetailMap": {
            "64a1b2c3d4e5f60718293a4b": {
                "note": {
                    "noteId": "64a1b2c3d4e5f60718293a4b",
                    "title": "测试笔记",
                    "desc": "描述文案 #测试",
                    "user": {"nickname": "测试作者"},
                    "interactInfo": {"likedCount": "1234", "collectedCount": "56", "commentCount": "7", "shareCount": "8"},
                    "imageList": [{"urlDefault": "https://sns-webpic.xhscdn.com/cover.jpg"}],
                    "video": {
                        "capa": {"duration": 42},
                        "media": {
                            "stream": {
                                "h264": [
                                    {"masterUrl": "https://sns-video-bd.xhscdn.com/1080.mp4", "height": 1080, "videoBitrate": 3000000},
                                    {"masterUrl": "https://sns-video-bd.xhscdn.com/720.mp4", "height": 720, "videoBitrate": 1200000},
                                ],
                                "h265": [{"masterUrl": "https://sns-video-bd.xhscdn.com/hevc.mp4", "height": 1080, "videoBitrate": 1500000}],
                            }
                        },
                        "consumer": {"originVideoKey": "origin/key/abc.mp4"},
                    },
                }
            }
        }
    }
}


def _state_html(state: dict) -> str:
    return f"<html><script>window.__INITIAL_STATE__={json.dumps(state, ensure_ascii=False)};</script></html>"


def test_streams_from_synthetic_video_state():
    video = SYNTHETIC_VIDEO_STATE["note"]["noteDetailMap"]["64a1b2c3d4e5f60718293a4b"]["note"]["video"]
    streams = Xiaohongshu._streams(video)
    qualities = [s.quality for s in streams]
    assert "origin" in qualities
    assert any(s.quality == "1080p" for s in streams)
    assert [s.rank for s in streams] == sorted((s.rank for s in streams), reverse=True)
    assert all(s.headers.get("Referer", "").startswith("https://www.xiaohongshu.com") for s in streams)


def test_find_note_from_synthetic_state():
    note = Xiaohongshu._find_note(SYNTHETIC_VIDEO_STATE)
    assert note is not None
    assert note["noteId"] == "64a1b2c3d4e5f60718293a4b"
    assert note["user"]["nickname"] == "测试作者"


def test_note_id_and_token_extraction():
    url = "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9?xsec_token=ABtokenCD&xsec_source=pc_feed"
    assert Xiaohongshu._note_id(url) == "6411cf99000000001300b6d9"
    assert Xiaohongshu._xsec_token(url) == "ABtokenCD"
    assert Xiaohongshu._note_id("https://www.xiaohongshu.com/discovery/item/6411cf99000000001300b6d9") == "6411cf99000000001300b6d9"


def test_real_dead_note_fixture_has_no_note_data(fixture):
    """真实抓的失效笔记页：INITIAL_STATE 在，但 note 数据不在 → 解析不出笔记（不崩）。"""
    state = extract_js_object(fixture("xhs_note_page.html"), "window.__INITIAL_STATE__=")
    assert Xiaohongshu._find_note(state) is None


def test_missing_state_marker_reports_upstream_change():
    with pytest.raises(UpstreamChanged):
        extract_js_object("<html>nothing</html>", "window.__INITIAL_STATE__=")


def test_no_video_streams_is_empty():
    assert Xiaohongshu._streams({}) == []


def test_no_media_error_mentions_token():
    err = NoMedia("x", hint="① 分享链接里的 xsec_token 丢了", platform="xhs")
    assert "xsec_token" in err.hint