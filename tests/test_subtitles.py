"""字幕：挑轨、转 SRT、落盘、与下载链路衔接。

实测背景（2026-10-25）：B站 `x/player/wbi/v2` 的 `subtitle.subtitles[]`
**匿名恒为空**（`need_login_subtitle=true`），带 SESSDATA 后同一条视频能拿到 1~12 轨。
所以这一层的契约是：没字幕 → 报错并说清"要登录"；有字幕 → 落成能被播放器/剪辑软件读的 .srt。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vextract import download
from vextract.errors import DownloadFailed
from vextract.models import MediaInfo, Stream, Subtitle


def media_with_subs(*langs: str) -> MediaInfo:
    return MediaInfo(
        platform="bilibili",
        media_id="BV1",
        source_url="https://www.bilibili.com/video/BV1",
        title="标题",
        streams=[Stream(url="https://cdn/v.mp4", quality="480P", rank=download.rank_of("480P", 480))],
        subtitles=[
            Subtitle(lan=lan, lan_doc=lan, url=f"https://aisubtitle.hdslb.com/{lan}.json", ai=lan.startswith("ai-"))
            for lan in langs
        ],
    )


# ---------- 挑轨 ----------
def test_auto_prefers_human_simplified_chinese():
    media = media_with_subs("en-US", "ai-zh", "zh-Hant", "zh-CN")
    picked = download.pick_subtitles(media, None)
    assert [s.lan for s in picked] == ["zh-CN"], "人工简体优先于 AI 中文与繁体"


def test_auto_falls_back_through_the_preference_list():
    assert download.pick_subtitles(media_with_subs("en-US", "ai-zh"))[0].lan == "ai-zh"
    assert download.pick_subtitles(media_with_subs("ja", "ko"))[0].lan == "ja", "都没有就取第一条"


def test_explicit_langs_are_respected_in_order():
    media = media_with_subs("zh-CN", "en-US", "ja")
    assert [s.lan for s in download.pick_subtitles(media, ["en-US", "ja"])] == ["en-US", "ja"]


def test_unknown_lang_lists_what_is_available():
    media = media_with_subs("zh-CN", "en-US")
    with pytest.raises(DownloadFailed) as err:
        download.pick_subtitles(media, ["fr"])
    assert "fr" in err.value.message
    assert "zh-CN" in (err.value.hint or "") and "en-US" in (err.value.hint or "")


def test_no_subtitles_explains_the_login_gate():
    """匿名查询恒为空——不能只说"没有字幕"，要说清怎么才能有。"""
    with pytest.raises(DownloadFailed) as err:
        download.pick_subtitles(MediaInfo(platform="bilibili", media_id="1", source_url="u", title="t"))
    hint = err.value.hint or ""
    assert "SESSDATA" in hint and "登录" in hint
    assert "--transcribe" in hint, "还要给出本地 ASR 这条替代路径"


# ---------- 落盘 ----------
def stub_subtitle_http(monkeypatch, rows=None):
    payload = {
        "body": rows
        if rows is not None
        else [
            {"from": 0.0, "to": 1.5, "location": 2, "content": "第一句"},
            {"from": 1.5, "to": 3.25, "location": 2, "content": "第二句"},
        ]
    }
    seen: list[str] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            seen.append(url)
            return FakeResponse()

    monkeypatch.setattr(download.httpx, "AsyncClient", FakeClient)
    return seen


@pytest.mark.asyncio
async def test_save_subtitles_writes_srt_with_real_timestamps(monkeypatch, tmp_path: Path):
    seen = stub_subtitle_http(monkeypatch)
    written = await download.save_subtitles(media_with_subs("zh-CN"), out_dir=tmp_path, stem="标题")

    assert len(written) == 1
    dest = written[0]
    assert dest.name == "标题.zh-CN.srt"
    body = dest.read_text(encoding="utf-8")
    assert body.splitlines()[:3] == ["1", "00:00:00,000 --> 00:00:01,500", "第一句"]
    assert "00:00:01,500 --> 00:00:03,250" in body
    assert seen == ["https://aisubtitle.hdslb.com/zh-CN.json"], "只该请求挑中的那一条"


@pytest.mark.asyncio
async def test_save_subtitles_downloads_every_requested_language(monkeypatch, tmp_path: Path):
    stub_subtitle_http(monkeypatch)
    written = await download.save_subtitles(
        media_with_subs("zh-CN", "en-US"), out_dir=tmp_path, stem="标题", langs=["zh-CN", "en-US"]
    )
    assert [p.name for p in written] == ["标题.zh-CN.srt", "标题.en-US.srt"]


@pytest.mark.asyncio
async def test_empty_subtitle_track_is_an_error(monkeypatch, tmp_path: Path):
    stub_subtitle_http(monkeypatch, rows=[])
    with pytest.raises(DownloadFailed) as err:
        await download.save_subtitles(media_with_subs("zh-CN"), out_dir=tmp_path, stem="x")
    assert "空" in err.value.message


@pytest.mark.asyncio
async def test_existing_srt_is_not_clobbered(monkeypatch, tmp_path: Path):
    stub_subtitle_http(monkeypatch)
    (tmp_path / "x.zh-CN.srt").write_text("用户手改过", encoding="utf-8")
    written = await download.save_subtitles(media_with_subs("zh-CN"), out_dir=tmp_path, stem="x")
    assert written[0].name == "x.zh-CN_2.srt"
    assert (tmp_path / "x.zh-CN.srt").read_text(encoding="utf-8") == "用户手改过"


# ---------- 与下载链路衔接 ----------
def stub_fetch(monkeypatch):
    async def fake_fetch(stream, dest, label, progress, cancel=None, mode="video", message=""):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 16)
        return 16

    monkeypatch.setattr(download, "_fetch", fake_fetch)


@pytest.mark.asyncio
async def test_download_saves_subtitles_beside_the_video(monkeypatch, tmp_path: Path):
    stub_fetch(monkeypatch)
    stub_subtitle_http(monkeypatch)
    artifacts: list[Path] = []
    stages: list[str] = []

    path = await download.download(
        media_with_subs("zh-CN"),
        out_dir=tmp_path,
        mode="video",
        subtitle_langs=["auto"],
        progress=lambda p: stages.append(p.stage),
        on_artifact=artifacts.append,
    )

    assert path.suffix == ".mp4"
    assert [p.name for p in artifacts] == [f"{path.stem}.zh-CN.srt"]
    assert "字幕" in stages, "字幕阶段要能被前端看见"


@pytest.mark.asyncio
async def test_download_without_subtitles_flag_writes_no_srt(monkeypatch, tmp_path: Path):
    stub_fetch(monkeypatch)
    stub_subtitle_http(monkeypatch)
    path = await download.download(media_with_subs("zh-CN"), out_dir=tmp_path, mode="video")
    assert list(tmp_path.glob("*.srt")) == []
    assert path.exists()
