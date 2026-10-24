"""快手：真实移动页 fixture（含 720p avc/hevc 双码流）解析。

背景：桌面页的 `__APOLLO_STATE__` 现在只有 UI 配置、桌面 GraphQL 要过验证码，
真正可用的链路是移动页 `window.INIT_STATE`（顶层键 -1 位移）。
"""

from __future__ import annotations

import pytest

from vextract.providers.kuaishou import Kuaishou, find_photo, parse_init_state, photo_id, unshift

URL = "https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa"


def test_unshift_restores_endpoint_name():
    assert unshift("tusjoh") == "string"
    assert unshift("qipup0tjnqmf0jogp") == "photo/simple/info"


def test_real_fixture_contains_photo_endpoint(fixture):
    """真实移动页里必须存在可位移还原出 photo/simple/info 的顶层键。"""
    state = parse_init_state(fixture("kuaishou_mobile.html"))
    decoded = [unshift(key) for key in state]
    assert any("photo/simple/info" in key for key in decoded)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa", "3xwzr5dveyqc5fa"),
        ("https://m.gifshow.com/fw/photo/3xabc", "3xabc"),
        ("https://www.kuaishou.com/?photoId=3xabc&x=1", "3xabc"),
        ("https://v.kuaishou.com/xYz123", "xYz123"),
    ],
)
def test_photo_id_extraction(url, expected):
    assert photo_id(url) == expected


def test_parse_real_mobile_state(fixture):
    photo = find_photo(parse_init_state(fixture("kuaishou_mobile.html")))
    assert photo is not None, "移动页 INIT_STATE 里应能定位到 photo/simple/info 节点"
    assert photo.get("caption")
    assert photo.get("userName")
    assert isinstance(photo.get("duration"), int)
    assert photo.get("mainMvUrls")


def test_streams_from_real_mobile_state(fixture):
    photo = find_photo(parse_init_state(fixture("kuaishou_mobile.html")))
    streams = Kuaishou._streams(photo)
    labels = [s.quality for s in streams]
    assert streams
    assert any(s.codec == "hevc" for s in streams), "manifest 里应有 hevc 码流"
    assert "origin" in labels, "mainMvUrls 应作为 origin 兜底档"
    assert [s.rank for s in streams] == sorted((s.rank for s in streams), reverse=True)
    assert all(s.headers.get("Referer") for s in streams)


def test_missing_photo_returns_none():
    assert find_photo({"some": {"other": "state"}}) is None