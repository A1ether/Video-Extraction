"""注册表：自动发现 + 优先级分发（新增平台不应影响这里的行为）。"""

from __future__ import annotations

import pytest

from vextract import registry
from vextract.errors import UnsupportedUrl
from vextract.providers.ytdlp_generic import YtdlpGeneric


def test_providers_are_discovered_by_module_scan():
    names = [p.name for p in registry.all_providers()]
    assert {"bilibili", "douyin", "kuaishou", "xhs", "weibo", "generic"} <= set(names)


def test_priority_order_puts_generic_last():
    providers = registry.all_providers()
    assert providers[-1].name == "generic"
    priorities = [p.priority for p in providers]
    assert priorities == sorted(priorities, reverse=True)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.bilibili.com/video/BV1GJ411x7h7/", "bilibili"),
        ("https://www.kuaishou.com/short-video/3x1", "kuaishou"),
        ("https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9", "xhs"),
        ("https://m.weibo.cn/detail/4189191225395228", "weibo"),
        ("https://v.douyin.com/iRNBho6u/", "douyin"),
        ("https://example.com/whatever", "generic"),
    ],
)
def test_dispatch(url, expected):
    picked = registry.pick(url)
    assert picked is not None and picked.name == expected


def test_generic_matches_any_http_url():
    assert YtdlpGeneric().matches("https://anything.example/v")
    assert not YtdlpGeneric().matches("ftp://anything.example/v")


def test_unsupported_url_without_any_match(monkeypatch):
    import asyncio

    monkeypatch.setattr(registry, "all_providers", lambda: ())
    with pytest.raises(UnsupportedUrl) as err:
        asyncio.run(registry.extract("https://example.com/x"))
    assert "vx platforms" in (err.value.hint or "")