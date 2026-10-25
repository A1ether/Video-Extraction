"""抖音 provider：cookie 阶梯 + "cookie 到位也没用"时给出真话。全部离线。"""

from __future__ import annotations

from pathlib import Path

import pytest

from vextract import config, cookies, engine
from vextract.errors import AuthRequired
from vextract.models import MediaInfo, Stream
from vextract.providers.douyin import Douyin

URL = "https://www.douyin.com/video/6961737553342991651"


@pytest.fixture
def provider() -> Douyin:
    return Douyin()


@pytest.fixture(autouse=True)
def no_browser_cookie_option(monkeypatch):
    monkeypatch.setattr(config.settings, "cookies_from_browser", None)


def fake_media() -> MediaInfo:
    return MediaInfo(
        platform="douyin",
        media_id="6961737553342991651",
        source_url=URL,
        title="标题",
        streams=[Stream(url="https://cdn/v.mp4", quality="720p", rank=720)],
    )


def test_matches_douyin_and_iesdouyin(provider: Douyin):
    assert provider.matches(URL)
    assert provider.matches("https://v.douyin.com/iRNBho6u/")
    assert provider.matches("https://www.iesdouyin.com/share/video/1/")
    assert not provider.matches("https://www.bilibili.com/video/BV1")


@pytest.mark.asyncio
async def test_without_cookie_tells_user_how_to_export(provider: Douyin, monkeypatch):
    monkeypatch.setattr(cookies, "cookie_file", lambda platform: None)
    with pytest.raises(AuthRequired) as exc:
        await provider.resolve(URL)
    assert "cookie" in exc.value.message
    assert "vx cookies import" in exc.value.hint  # 指引必须是可执行的下一步
    assert exc.value.platform == "douyin"


@pytest.mark.asyncio
async def test_engine_gets_the_cookie_file(provider: Douyin, monkeypatch, tmp_path: Path):
    jar = tmp_path / "douyin.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(cookies, "ytdlp_cookiefile", lambda platform: str(jar))
    seen: dict[str, object] = {}

    async def fake_extract(url, *, platform, cookiefile=None, from_browser=None):
        seen.update(url=url, platform=platform, cookiefile=cookiefile, from_browser=from_browser)
        return fake_media()

    monkeypatch.setattr(engine, "extract_with_ytdlp", fake_extract)
    media = await provider.resolve(URL)
    assert media.media_id == "6961737553342991651"
    assert seen == {"url": URL, "platform": "douyin", "cookiefile": str(jar), "from_browser": None}


@pytest.mark.asyncio
async def test_cookie_present_but_still_blocked_blames_the_signature(provider: Douyin, monkeypatch, tmp_path: Path):
    """cookie 已加载却仍被拒 → 必须指向 a_bogus 签名，而不是继续让用户折腾 cookie。"""
    jar = tmp_path / "douyin.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(cookies, "ytdlp_cookiefile", lambda platform: str(jar))

    async def blocked(url, **kwargs):
        raise AuthRequired("该平台要求浏览器 cookie（不是登录态也常常需要）", detail="Fresh cookies are needed")

    monkeypatch.setattr(engine, "extract_with_ytdlp", blocked)
    with pytest.raises(AuthRequired) as exc:
        await provider.resolve(URL)
    hint = exc.value.hint
    assert "a_bogus" in hint and "UIFID" in hint and "Argus" in hint
    assert exc.value.detail == "Fresh cookies are needed"  # 上游原文留在 detail，便于排查
    assert "vx cookies import" not in hint  # 别再教用户导 cookie：他已经导过了


@pytest.mark.asyncio
async def test_cookies_from_browser_counts_as_a_cookie(provider: Douyin, monkeypatch):
    """--cookies-from-browser 也算阶梯里的一档，不该先被 cookie 检查拦下。"""
    monkeypatch.setattr(config.settings, "cookies_from_browser", "chrome")
    monkeypatch.setattr(cookies, "cookie_file", lambda platform: None)
    seen: dict[str, object] = {}

    async def fake_extract(url, *, platform, cookiefile=None, from_browser=None):
        seen.update(cookiefile=cookiefile, from_browser=from_browser)
        return fake_media()

    monkeypatch.setattr(engine, "extract_with_ytdlp", fake_extract)
    await provider.resolve(URL)
    assert seen == {"cookiefile": None, "from_browser": "chrome"}