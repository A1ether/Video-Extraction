"""抖音 provider：自研签名 + 身份闸门。全部离线（fixture 驱动），不碰网络。"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from vextract import config, cookies
from vextract.errors import AuthRequired, NoMedia, RateLimited, UpstreamChanged
from vextract.providers.douyin import (
    SIGNATURE_SALT,
    Douyin,
    aweme_id_of,
    encode_pairs,
    sign_query,
)

FIXTURES = Path(__file__).parent / "fixtures"
# autouse fixture 会把 _ttwid 换成假的；想在测试里测真的那份，就先在导入时抓住它。
_ORIGINAL_TTWID = Douyin._ttwid
URL = "https://www.douyin.com/video/6961737553342991651"


@pytest.fixture
def provider() -> Douyin:
    return Douyin()


@pytest.fixture
def detail() -> dict:
    return json.loads((FIXTURES / "douyin_detail.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def cookie_jar(monkeypatch, tmp_path: Path):
    """把 cookie 目录指到 tmp，并放一份只含 UIFID 的导出（模拟用户导入过）。"""
    path = tmp_path / "douyin.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".douyin.com\tTRUE\t/\tTRUE\t1823151027\tUIFID\t" + "a" * 320 + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cookies, "cookie_file", lambda platform: path if platform == "douyin" else None)
    yield path


@pytest.fixture(autouse=True)
def guest_ttwid(monkeypatch):
    """ttwid 走缓存，不真的去打注册接口。"""

    async def fake(self) -> str:
        return "1%7Cguest-ttwid"

    monkeypatch.setattr(Douyin, "_ttwid", fake)


# ---------- URL → 作品 ID ----------
@pytest.mark.parametrize(
    ("url", "expect"),
    [
        ("https://www.douyin.com/video/6961737553342991651", "6961737553342991651"),
        ("https://www.douyin.com/video/6961737553342991651?previous_page=x", "6961737553342991651"),
        ("https://www.iesdouyin.com/share/video/7298145681699622182/?region=CN", "7298145681699622182"),
        ("https://www.douyin.com/note/7278279725944507659", "7278279725944507659"),
        ("https://www.douyin.com/discover?modal_id=6961737553342991651", "6961737553342991651"),
        ("https://www.douyin.com/user/MS4wLjABAAAA", None),
    ],
)
def test_aweme_id_of(url: str, expect: str | None):
    assert aweme_id_of(url) == expect


def test_matches_only_douyin(provider: Douyin):
    assert provider.matches(URL)
    assert provider.matches("https://v.douyin.com/iRNBho6u/")
    assert not provider.matches("https://www.bilibili.com/video/BV1")


# ---------- 签名 ----------
def test_signature_matches_pinned_vector():
    """格式护栏：输入拼接方式（顺序、分隔符、转义、salt）一旦改动就会红。"""
    golden = json.loads((FIXTURES / "douyin_signature_golden.json").read_text(encoding="utf-8"))
    assert sign_query(golden["query"], golden["uifid"], golden["timestamp"]) == golden["signature"]


def test_signature_is_a_pure_function_of_its_inputs():
    """无 nonce 无状态：同输入同输出（这也正是它能离线复算的原因）。"""
    a = sign_query("aweme_id=1", "u" * 10, 1792870001)
    b = sign_query("aweme_id=1", "u" * 10, 1792870001)
    assert a == b and len(a) == 32
    assert sign_query("aweme_id=1", "u" * 10, 1792870002) != a  # 时间戳变化必须改变签名


def test_signature_is_32_hex():
    sig = sign_query("aweme_id=1", "x", 1)
    assert len(sig) == 32 and all(c in "0123456789abcdef" for c in sig)


def test_query_encoding_matches_urlsearchparams():
    """`*-._` 不转义、空格变 %20 —— 签名与发出去的 URL 必须是同一串字节。"""
    assert encode_pairs([("a b", "c*d-e.f_g")]) == "a%20b=c*d-e.f_g"
    assert encode_pairs([("k", "中文")]) == "k=%E4%B8%AD%E6%96%87"


def test_salt_is_the_documented_constant():
    assert SIGNATURE_SALT == "A96D855A08C0A9707F8BEF0D9A527E4E"


# ---------- 身份闸门 ----------
@pytest.mark.asyncio
async def test_missing_uifid_asks_for_a_browser_identity(provider: Douyin, monkeypatch):
    monkeypatch.setattr(cookies, "cookie_file", lambda platform: None)
    with pytest.raises(AuthRequired) as exc:
        await provider.resolve(URL)
    assert "UIFID" in exc.value.message
    assert "vx cookies import" in exc.value.hint


@pytest.mark.asyncio
async def test_no_aweme_id_is_no_media(provider: Douyin):
    with pytest.raises(NoMedia) as exc:
        await provider.resolve("https://www.douyin.com/user/MS4wLjABAAAA")
    assert "作品 ID" in exc.value.message


# ---------- 成功路径 ----------
@pytest.mark.asyncio
async def test_resolve_uses_uifid_and_signature(provider: Douyin, monkeypatch, detail: dict):
    seen: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = json.dumps({"aweme_detail": detail})

    async def fake_get(self, url, **kwargs):
        seen["url"] = url
        seen["cookies"] = {c.name: c.value for c in self.cookies.jar}
        seen["referer"] = self.headers.get("Referer")
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    media = await provider.resolve(URL)

    assert media.platform == "douyin"
    assert media.media_id == "6961737553342991651"
    assert media.title.startswith("#杨超越")
    assert media.author == "杨超越"
    assert media.duration == pytest.approx(19.782)
    assert media.needs_merge is False
    assert media.stats["like"] == 323278

    url = str(seen["url"])
    assert "x-secsdk-web-signature=" in url and "a_bogus" not in url  # a_bogus 不是必需项
    params = dict(parse_qsl(url.split("?")[1]))
    assert params["uifid"] == "a" * 320
    assert len(params["x-secsdk-web-signature"]) == 32
    assert seen["cookies"]["UIFID"] == "a" * 320
    assert seen["cookies"]["ttwid"] == "1%7Cguest-ttwid"
    assert str(seen["referer"]).startswith("https://www.douyin.com")  # 缺 Referer 会拿到空 body


def test_streams_cover_every_gear_and_prefer_high_bitrate(provider: Douyin, detail: dict):
    media = provider._to_media(detail, "6961737553342991651")
    labels = [s.quality for s in media.streams]
    assert labels[0].startswith("1080p")  # 最高档排在前面
    assert any("720p" in label for label in labels)
    assert all(s.headers.get("Referer") for s in media.streams)  # 直链裸请求 403，必须带 Referer
    assert media.streams[0].rank > media.streams[-1].rank
    assert media.streams[0].filesize and media.streams[0].filesize > 1_000_000


def test_lower_gear_is_marked_so_users_do_not_pick_it_as_best(provider: Douyin, detail: dict):
    media = provider._to_media(detail, "6961737553342991651")
    low = [s for s in media.streams if "低码率" in s.quality]
    assert low, "lower_540_0 应当被标注为低码率"
    assert low[0].rank < media.streams[0].rank


def test_images_only_post_becomes_assets(provider: Douyin, detail: dict):
    """图文作品以前被当成错误（NO_MEDIA），现在解析成图集——行为变更见 docs/03。"""
    post = {
        **detail,
        "video": {},
        "images": [{"url_list": ["https://x/1.jpg", "https://y/1.jpg"], "width": 1080, "height": 1440}],
    }
    media = provider._to_media(post, "1")
    assert media.is_image_post and media.streams == []
    assert [a.url for a in media.assets] == ["https://x/1.jpg"], "url_list 取第一个直链"
    assert media.assets[0].width == 1080 and media.assets[0].height == 1440
    assert "图集" in (media.quality_note or "")


def test_post_with_neither_video_nor_images_is_no_media(provider: Douyin, detail: dict):
    post = {**detail, "video": {}, "images": []}
    with pytest.raises(NoMedia):
        provider._to_media(post, "1")


# ---------- 失败路径 ----------
def _respond(monkeypatch, *, status: int, text: str):
    class FakeResponse:
        status_code = status

    FakeResponse.text = text

    async def fake_get(self, url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


@pytest.mark.asyncio
async def test_argus_rejection_is_rate_limited_with_actionable_hint(provider: Douyin, monkeypatch):
    _respond(monkeypatch, status=403, text="Blocked by ArgusSecurityPlugin Signature Not Found")
    with pytest.raises(RateLimited) as exc:
        await provider.resolve(URL)
    assert "Argus" in exc.value.message
    assert "vx cookies import" in exc.value.hint  # 下一步必须可执行


@pytest.mark.asyncio
async def test_empty_body_is_upstream_changed_not_no_media(provider: Douyin, monkeypatch):
    """200 + 0 字节是 Referer 被剥离的典型症状，不能误报成"作品不存在"。"""
    _respond(monkeypatch, status=200, text="")
    with pytest.raises(UpstreamChanged) as exc:
        await provider.resolve(URL)
    assert "空响应" in exc.value.message


@pytest.mark.asyncio
async def test_missing_detail_reports_the_platform_reason(provider: Douyin, monkeypatch):
    body = json.dumps(
        {
            "aweme_detail": None,
            "filter_detail": {"aweme_id": "1", "detail_msg": "因作品权限或已被删除，无法观看", "filter_reason": "status_deleted"},
        }
    )
    _respond(monkeypatch, status=200, text=body)
    with pytest.raises(NoMedia) as exc:
        await provider.resolve(URL)
    assert "已被删除" in exc.value.message
    assert "status_deleted" in (exc.value.detail or "")


@pytest.mark.asyncio
async def test_non_json_is_upstream_changed(provider: Douyin, monkeypatch):
    _respond(monkeypatch, status=200, text="<html>challenge</html>")
    with pytest.raises(UpstreamChanged):
        await provider.resolve(URL)

# ---------- 身份来源阶梯 ----------
def test_browser_source_is_used_when_no_cookie_file(provider: Douyin, monkeypatch):
    """L2：没有 L1 文件时，允许从本机浏览器只读地取 UIFID（--cookies-from-browser）。"""
    import yt_dlp.cookies as yc

    monkeypatch.setattr(cookies, "cookie_file", lambda platform: None)
    monkeypatch.setattr(config.settings, "cookies_from_browser", "chrome")

    class FakeCookie:
        def __init__(self, name, value):
            self.name, self.value = name, value

    class FakeJar(list):
        pass

    monkeypatch.setattr(
        yc, "extract_cookies_from_browser", lambda browser: FakeJar([FakeCookie("sid_tt", "x"), FakeCookie("UIFID", "b" * 320)])
    )
    assert Douyin._uifid() == "b" * 320


def test_browser_source_absent_returns_none(provider: Douyin, monkeypatch):
    monkeypatch.setattr(cookies, "cookie_file", lambda platform: None)
    monkeypatch.setattr(config.settings, "cookies_from_browser", None)
    assert Douyin._uifid() is None


def test_browser_read_failure_is_not_fatal(provider: Douyin, monkeypatch):
    """Chrome 数据库被锁 / 钥匙串没授权 —— 当作"没有 UIFID"，走正常报错而不是崩。"""
    import yt_dlp.cookies as yc

    monkeypatch.setattr(cookies, "cookie_file", lambda platform: None)
    monkeypatch.setattr(config.settings, "cookies_from_browser", "chrome")

    def boom(browser):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(yc, "extract_cookies_from_browser", boom)
    assert Douyin._uifid() is None


def test_l1_file_wins_over_browser(provider: Douyin, monkeypatch):
    """用户显式导入的 cookie 优先，不去翻浏览器。"""
    import yt_dlp.cookies as yc

    monkeypatch.setattr(config.settings, "cookies_from_browser", "chrome")
    monkeypatch.setattr(yc, "extract_cookies_from_browser", lambda browser: pytest.fail("不该读浏览器"))
    assert Douyin._uifid() == "a" * 320


# ---------- ttwid 缓存 ----------
@pytest.mark.asyncio
async def test_ttwid_is_cached_and_reused(provider: Douyin, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config.settings, "home", tmp_path)
    calls = {"n": 0}

    class FakeResponse:
        status_code = 200

        class cookies:
            @staticmethod
            def get(name):
                return "guest-token-1"

    async def fake_post(self, url, **kwargs):
        calls["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    real = _ORIGINAL_TTWID.__get__(provider)  # 绕开 autouse 的假实现

    assert await real() == "guest-token-1"
    assert await real() == "guest-token-1"
    assert calls["n"] == 1, "第二次必须走缓存，不再打注册接口"
    cache = tmp_path / "ttwid-cache"
    assert cache.exists() and cache.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_stale_ttwid_cache_is_ignored(provider: Douyin, monkeypatch, tmp_path: Path):
    import os
    import time as time_module

    monkeypatch.setattr(config.settings, "home", tmp_path)
    cache = tmp_path / "ttwid-cache"
    cache.write_text("old-token", encoding="utf-8")
    old = time_module.time() - 4 * 24 * 3600
    os.utime(cache, (old, old))

    class FakeResponse:
        status_code = 200

        class cookies:
            @staticmethod
            def get(name):
                return "fresh-token"

    async def fake_post(self, url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    real = _ORIGINAL_TTWID.__get__(provider)  # 绕开 autouse 的假实现
    assert await real() == "fresh-token"  # 超过 3 天不复用


@pytest.mark.asyncio
async def test_ttwid_registration_failure_is_reported_not_swallowed(provider: Douyin, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config.settings, "home", tmp_path)

    async def boom(self, url, **kwargs):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    real = _ORIGINAL_TTWID.__get__(provider)  # 绕开 autouse 的假实现
    with pytest.raises(UpstreamChanged) as exc:
        await real()
    assert "ttwid" in exc.value.message
