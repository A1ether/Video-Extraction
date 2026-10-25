"""真机测试：默认跳过（pyproject 里 addopts = -m 'not live'）。

跑法：`uv run pytest -m live`
这些用例访问真实平台，用来发现"平台改版导致解析失效"——也就是本项目最大的风险。

抖音用例需要一个真实 UIFID：本机 ~/.config/vextract/cookies/douyin.txt 里已有就直接跑，
没有就 skip（不把"环境缺失"混成"平台坏了"）。
"""

from __future__ import annotations

import os

import pytest

from vextract import asr, cookies, download, registry

pytestmark = pytest.mark.live

BILI_URL = "https://www.bilibili.com/video/BV1GJ411x7h7/"
WEIBO_URL = "https://m.weibo.cn/detail/4189191225395228"
# 真实图文微博样本（18 张图）：音频/视频都没有，只有 pic_infos —— 图集能力的正例
WEIBO_IMAGES_URL = "https://m.weibo.cn/status/5357907014320571"
KS_URL = "https://www.kuaishou.com/short-video/3xwzr5dveyqc5fa"
DY_URL = "https://www.douyin.com/video/6961737553342991651"


async def _extract(url: str):
    return await registry.extract(url)


@pytest.mark.asyncio
async def test_bilibili_live():
    media = await _extract(BILI_URL)
    assert media.platform == "bilibili"
    assert any(not s.audio_only for s in media.streams)
    assert media.needs_merge is True


@pytest.mark.asyncio
async def test_weibo_live():
    media = await _extract(WEIBO_URL)
    assert media.platform == "weibo"
    assert media.streams


@pytest.mark.asyncio
async def test_kuaishou_live():
    media = await _extract(KS_URL)
    assert media.platform == "kuaishou"
    assert any(s.codec == "hevc" for s in media.streams)


def _has_uifid() -> bool:
    path = cookies.cookie_file("douyin")
    if not path:
        return False
    names = {name for _domain, name, _value in cookies.load_netscape(path)}
    return bool(names & {"UIFID", "UIFID_TEMP", "uifid", "uifid_temp"})


@pytest.mark.asyncio
async def test_douyin_live_needs_only_a_browser_identity():
    """抖音：给了 UIFID 就该真的解析出来（自研签名 + 游客 ttwid）。

    这条用例的价值在于它会在平台改签名/改闸门时先红：一旦失败，
    要么重新抓 UIFID，要么说明 Argus 规则变了。
    """
    if not _has_uifid():
        pytest.skip("没有抖音 UIFID：先 `vx cookies import <导出文件> --platform douyin`")

    media = await _extract(DY_URL)
    assert media.platform == "douyin"
    assert media.media_id == "6961737553342991651"
    video = [s for s in media.streams if not s.audio_only]
    assert video, "应当至少有一路视频流"
    assert video[0].headers.get("Referer"), "抖音直链裸请求 403，必须带 Referer"
    assert media.duration and media.duration > 0

# ---------- 音频 / 图集 / 本地转录（本轮新增能力）----------
@pytest.mark.asyncio
async def test_bilibili_live_audio_track_is_separate():
    """B站自带纯音频轨：音频模式直接下它，不需要从视频里抽。"""
    media = await _extract(BILI_URL)
    stream, extract = download.pick_audio(media)
    assert stream.audio_only is True and extract is False
    assert stream.bitrate and stream.bitrate > 0
    assert stream.ext == "m4a", "自带音轨落成 m4a 才可能零重编码"


@pytest.mark.asyncio
async def test_weibo_live_image_post_yields_assets():
    """图文微博（视频/音频都为空）必须给 assets——这条是图集能力的线上正例。"""
    media = await _extract(WEIBO_IMAGES_URL)
    assert media.is_image_post, f"应当识别成图文作品，实际 streams={media.streams} assets={media.assets}"
    assert len(media.assets) >= 2
    assert [a.index for a in media.assets] == list(range(1, len(media.assets) + 1))
    first = media.assets[0]
    assert first.url.startswith("https://") and first.width and first.height
    assert "weibo.com" in (first.headers.get("Referer") or ""), "微博图片 CDN 也要 Referer"


@pytest.mark.asyncio
async def test_weibo_live_image_download_writes_numbered_folder(tmp_path):
    """真下一遍：图片落成 01.jpg… 的同名文件夹，每张都有真实内容。"""
    media = await _extract(WEIBO_IMAGES_URL)
    folder = await download.download(media, out_dir=tmp_path, mode="images")

    assert folder.is_dir()
    files = sorted(item.name for item in folder.iterdir())
    assert len(files) == len(media.assets)
    assert files[0] == "01.jpg" and files[-1] == f"{len(media.assets):02d}.jpg"
    assert all((folder / name).stat().st_size > 5_000 for name in files)


@pytest.mark.asyncio
async def test_douyin_live_audio_download_extracts_and_tags(tmp_path):
    """抖音是音视频一体的 mp4：音频模式要用 ffmpeg 抽出音轨（并写入标签）。"""
    if not _has_uifid():
        pytest.skip("没有抖音 UIFID：先 `vx cookies import <导出文件> --platform douyin`")

    media = await _extract(DY_URL)
    path = await download.download(media, out_dir=tmp_path, mode="audio", audio_format="m4a", embed_tags=True)

    assert path.suffix == ".m4a" and path.stat().st_size > 50_000
    codec = download._audio_codec(path)
    assert codec and "h264" not in codec, f"抽出来的应当是音轨，实际编码 {codec}"


@pytest.mark.asyncio
async def test_douyin_live_transcription(tmp_path):
    """本地转录端到端：音频 → 文字稿。

    模型权重很重（默认 large-v3-turbo 约 1.6GB），所以默认跳过；要跑就：
    `VEXTRACT_LIVE_ASR=1 VEXTRACT_LIVE_ASR_MODEL=mlx-community/whisper-tiny-mlx uv run pytest -m live -k transcribe`
    """
    if os.environ.get("VEXTRACT_LIVE_ASR") != "1":
        pytest.skip("ASR 模型下载很重：设 VEXTRACT_LIVE_ASR=1 才跑")
    if not asr.available_backends():
        pytest.skip("没装 ASR 后端：uv sync --extra transcribe")
    if not _has_uifid():
        pytest.skip("没有抖音 UIFID")

    media = await _extract(DY_URL)
    audio = await download.download(media, out_dir=tmp_path, mode="audio", embed_tags=False)
    written = await asr.transcribe_to_files(
        media, audio, model=os.environ.get("VEXTRACT_LIVE_ASR_MODEL") or None, language="zh"
    )

    assert len(written) == 1 and written[0].suffix == ".md"
    body = written[0].read_text(encoding="utf-8")
    assert body.startswith("# ") and media.source_url in body
    assert len(body) > 80, f"文字稿太短，可能是空转：{body!r}"
