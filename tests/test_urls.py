"""分享文本 → 平台 → URL 的入口逻辑（用户粘贴的是口令，不是干净 URL）。"""

from __future__ import annotations

import pytest

from vextract import urls
from vextract.errors import UrlNotFound


def test_extract_url_from_chinese_share_text():
    text = "7.92 复制打开抖音，看看【某人的作品】https://v.douyin.com/iRNBho6u/ 复制此链接"
    assert urls.extract_url(text) == "https://v.douyin.com/iRNBho6u/"


def test_extract_url_strips_trailing_punctuation():
    assert urls.extract_url("看这个 https://www.bilibili.com/video/BV1GJ411x7h7/。") == (
        "https://www.bilibili.com/video/BV1GJ411x7h7/"
    )


def test_extract_url_missing_raises_with_hint():
    with pytest.raises(UrlNotFound) as err:
        urls.extract_url("纯文字没有链接")
    assert err.value.code == "URL_NOT_FOUND"
    assert "分享" in (err.value.hint or "")


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://www.bilibili.com/video/BV1GJ411x7h7/", "bilibili"),
        ("https://b23.tv/abcDEF", "bilibili"),
        ("https://v.douyin.com/iRNBho6u/", "douyin"),
        ("https://www.kuaishou.com/short-video/3xabc", "kuaishou"),
        ("https://v.kuaishou.com/xYz123", "kuaishou"),
        ("https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9", "xhs"),
        ("https://xhslink.com/a/abc123", "xhs"),
        ("https://m.weibo.cn/detail/5120561132606436", "weibo"),
        ("https://weibo.com/7827771738/N4xlMvjhI", "weibo"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://www.tiktok.com/@u/video/123", "tiktok"),
        ("https://vm.tiktok.com/abc", "tiktok"),
        ("https://example.com/v/1", None),
    ],
)
def test_platform_detection(url, platform):
    assert urls.platform_of(url) == platform


def test_short_link_detection():
    assert urls.is_short("https://b23.tv/abc") is True
    assert urls.is_short("https://www.bilibili.com/video/BV1") is False