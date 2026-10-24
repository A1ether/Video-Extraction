"""微博：bid→mid 转换（用真实 bid/mid 对作为黄金值）+ 真实 statuses/show 响应解析。"""

from __future__ import annotations

import json

import pytest

from vextract.providers.weibo import Weibo, bid2mid, mid_from_url


@pytest.mark.parametrize(
    ("bid", "mid"),
    [
        # 实测：weibo.com/7827771738/N4xlMvjhI 用该 mid 请求 statuses/show 成功返回同一条微博
        ("N4xlMvjhI", "4910815147462302"),
        ("OBmHSEANZ", "5089054249674603"),
    ],
)
def test_bid2mid_golden(bid, mid):
    assert bid2mid(bid) == mid


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://m.weibo.cn/detail/4189191225395228", "4189191225395228"),
        ("https://m.weibo.cn/status/4189191225395228", "4189191225395228"),
        ("https://weibo.com/7827771738/N4xlMvjhI", "4910815147462302"),
        ("https://weibo.com/", None),
    ],
)
def test_mid_from_url(url, expected):
    assert mid_from_url(url) == expected


def test_parse_real_status_fixture(fixture):
    data = json.loads(fixture("weibo_status.json"))
    media = data["page_info"]["media_info"]
    streams = Weibo._streams(media)
    assert streams, "fixture 必须含至少一条视频流"
    assert all("weibo.com" in (s.headers.get("Referer") or "") for s in streams)
    # 排序必须显式：微博 playback_list 的返回顺序与实际清晰度不一致
    assert [s.rank for s in streams] == sorted((s.rank for s in streams), reverse=True)


def test_no_video_yields_no_media_hint():
    streams = Weibo._streams({})
    assert streams == []