"""音频模式：选流、零重编码快通道、转码参数、标签、模式推断。

这一层的核心风险是"静默产出坏文件"：把视频轨当音频轨下、该转码却 copy 了、
或者悄悄降级成"最佳视频"。所以每条断言都盯住"产物到底是什么"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vextract import download
from vextract.errors import DownloadFailed
from vextract.models import Asset, MediaInfo, Mode, Stream


def dash_media(**overrides) -> MediaInfo:
    """DASH 平台：视频轨与音频轨分开（B站/YouTube）。"""
    streams = overrides.pop(
        "streams",
        [
            Stream(url="https://cdn/v1080.mp4", quality="1080P", rank=download.rank_of("1080P", 1080, 2_000_000)),
            Stream(url="https://cdn/v480.mp4", quality="480P", rank=download.rank_of("480P", 480, 600_000)),
            Stream(
                url="https://cdn/a64.m4a", quality="audio-64k", audio_only=True, ext="m4a", codec="aac",
                rank=download.rank_of("audio", bitrate=64_000),
            ),
            Stream(
                url="https://cdn/a132.m4a", quality="audio-132k", audio_only=True, ext="m4a", codec="aac",
                rank=download.rank_of("audio", bitrate=132_000),
            ),
        ],
    )
    return MediaInfo(
        platform="bilibili",
        media_id="BV1",
        source_url="https://www.bilibili.com/video/BV1",
        title="标题",
        author="作者",
        streams=streams,
        needs_merge=overrides.pop("needs_merge", True),
        **overrides,
    )


def single_file_media(**overrides) -> MediaInfo:
    """单文件 mp4 平台：音视频一体（抖音/微博/快手/小红书）。"""
    streams = overrides.pop(
        "streams",
        [
            Stream(
                url="https://cdn/1080.mp4", quality="1080P", codec="h264",
                rank=download.rank_of("1080P", 1080, 1_800_000),
            )
        ],
    )
    return MediaInfo(
        platform="douyin",
        media_id="7",
        source_url="https://www.douyin.com/video/7",
        title="标题",
        author="作者",
        streams=streams,
        needs_merge=False,
        **overrides,
    )


# ---------- 模式推断 ----------
def test_resolve_mode_prefers_explicit_mode():
    assert download.resolve_mode(dash_media(), "images") == "images"
    assert download.resolve_mode(dash_media(), "audio") == "audio"


def test_quality_audio_switches_to_audio_mode():
    """Quality 里的 `audio` 档以前会被静默当成"最佳视频"，现在必须真的走音频模式。"""
    assert download.resolve_mode(dash_media(), None, quality="audio") == "audio"
    assert download.resolve_mode(single_file_media(), None, quality="audio") == "audio"
    assert download.resolve_mode(dash_media(), None, quality="best") == "video"


def test_image_post_infers_image_mode():
    media = MediaInfo(
        platform="douyin", media_id="1", source_url="u", title="图文",
        assets=[Asset(url="https://cdn/1.jpg", index=1)],
    )
    assert download.resolve_mode(media, None) == "images"
    assert media.is_image_post and media.work_kind == "图文作品"


# ---------- 选流 ----------
def test_pick_audio_uses_separate_track_without_extraction():
    stream, extract = download.pick_audio(dash_media())
    assert stream.url == "https://cdn/a132.m4a"
    assert extract is False, "平台自带音轨，不该再抽"


def test_pick_audio_falls_back_to_video_stream_for_single_file_platforms():
    stream, extract = download.pick_audio(single_file_media())
    assert stream.quality == "1080P"
    assert extract is True, "单文件 mp4 必须用 ffmpeg 抽音轨"


def test_pick_audio_respects_explicit_row():
    # 表格行号按"视频在前（rank 降序）、音频在后"排列：1=1080P 2=480P 3=audio-132k 4=audio-64k
    stream, extract = download.pick_audio(dash_media(), stream_index=4)
    assert stream.quality == "audio-64k" and extract is False

    stream, extract = download.pick_audio(dash_media(), stream_index=1)  # 1080P 那行
    assert stream.quality == "1080P" and extract is True


def test_pick_audio_without_any_stream_raises():
    with pytest.raises(DownloadFailed):
        download.pick_audio(MediaInfo(platform="x", media_id="1", source_url="u", title="t"))


def test_video_mode_on_image_post_raises_with_hint():
    media = MediaInfo(
        platform="xhs", media_id="1", source_url="u", title="图文",
        assets=[Asset(url="https://cdn/1.jpg", index=1)],
    )
    with pytest.raises(DownloadFailed) as err:
        download.pick_streams(media)
    assert "图集" in (err.value.hint or "")


# ---------- 转码参数 ----------
def test_audio_codec_args_copy_when_source_already_matches(monkeypatch):
    monkeypatch.setattr(download, "_audio_codec", lambda _p: "aac")
    assert download.audio_codec_args(Path("a.m4a"), "m4a") == ["-vn", "-c:a", "copy"]

    monkeypatch.setattr(download, "_audio_codec", lambda _p: "mp3")
    assert download.audio_codec_args(Path("a.mp3"), "mp3") == ["-vn", "-c:a", "copy"]


def test_audio_codec_args_transcodes_when_container_differs(monkeypatch):
    monkeypatch.setattr(download, "_audio_codec", lambda _p: "opus")
    assert download.audio_codec_args(Path("a.webm"), "m4a") == ["-vn", "-c:a", "aac", "-b:a", "192k"]
    assert download.audio_codec_args(Path("a.m4a"), "mp3") == ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
    assert download.audio_codec_args(Path("a.m4a"), "flac") == ["-vn", "-c:a", "flac"]


def test_audio_codec_args_transcodes_when_probe_unavailable(monkeypatch):
    """ffprobe 读不出来时宁可转码，也不赌一次 copy（会产出坏文件）。"""
    monkeypatch.setattr(download, "_audio_codec", lambda _p: None)
    assert download.audio_codec_args(Path("a.m4a"), "m4a")[1:3] == ["-c:a", "aac"]


def test_tag_args_only_includes_present_fields():
    args = download._tag_args(dash_media())
    assert "-metadata" in args
    assert "title=标题" in args and "artist=作者" in args
    assert download._tag_args(MediaInfo(platform="x", media_id="1", source_url="u", title="t")) == [
        "-metadata", "title=t", "-metadata", "comment=u",
    ]


# ---------- 落盘 ----------
def stub_fetch(monkeypatch, payload: bytes = b"x" * 32):
    """替掉 _fetch：写文件并像真实实现那样推两次进度（否则进度断言等于没测）。"""
    calls: list[tuple[str, str]] = []

    async def fake_fetch(stream, dest: Path, label, progress, cancel=None, mode: Mode = "video", message=""):
        calls.append((stream.url, label))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        if progress:
            for done in (0, len(payload)):
                progress(
                    download.Progress(
                        stage=label,
                        done=done,
                        total=len(payload),
                        overall=download._overall(label, done, len(payload), mode),
                        message=message,
                    )
                )
        return len(payload)

    monkeypatch.setattr(download, "_fetch", fake_fetch)
    return calls


@pytest.mark.asyncio
async def test_audio_fast_path_needs_no_ffmpeg_when_track_is_already_m4a(monkeypatch, tmp_path: Path):
    """B站自带音轨 + 要 m4a + 不写标签 → 纯改名，不请 ffmpeg（省一次重编码）。"""
    stub_fetch(monkeypatch)

    def explode():
        raise AssertionError("这条路径不该调用 ffmpeg")

    monkeypatch.setattr(download, "require_ffmpeg", explode)
    path = await download.download(dash_media(), out_dir=tmp_path, mode="audio", embed_tags=False)

    assert path.suffix == ".m4a" and path.exists()
    assert path.stat().st_size == 32
    assert path.name.endswith(".m4a")


@pytest.mark.asyncio
async def test_audio_transcode_invokes_ffmpeg_with_codec_and_tags(monkeypatch, tmp_path: Path):
    stub_fetch(monkeypatch)
    monkeypatch.setattr(download, "_audio_codec", lambda _p: "aac")
    captured: list[list[str]] = []

    async def fake_ffmpeg(args, *, what, hint):
        captured.append(args)
        Path(args[-1]).write_bytes(b"converted")

    monkeypatch.setattr(download, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(download, "require_ffmpeg", lambda: "/fake/ffmpeg")

    path = await download.download(dash_media(), out_dir=tmp_path, mode="audio", audio_format="mp3")

    assert path.suffix == ".mp3" and path.read_bytes() == b"converted"
    assert len(captured) == 1
    args = captured[0]
    assert "-c:a" in args and "libmp3lame" in args
    assert "title=标题" in args and "artist=作者" in args and "comment=https://www.bilibili.com/video/BV1" in args
    assert str(path) == args[-1]


@pytest.mark.asyncio
async def test_audio_mode_without_ffmpeg_reports_dependency(monkeypatch, tmp_path: Path):
    """要转码（mp3）却没有 ffmpeg → 必须报 DEPENDENCY_MISSING，而不是产出错文件。"""
    stub_fetch(monkeypatch)
    monkeypatch.setattr(download, "_audio_codec", lambda _p: "aac")
    monkeypatch.setattr(download.shutil, "which", lambda _: None)

    from vextract.errors import DependencyMissing

    with pytest.raises(DependencyMissing):
        await download.download(dash_media(), out_dir=tmp_path, mode="audio", audio_format="mp3")


@pytest.mark.asyncio
async def test_audio_mode_reports_stage_progress_within_range(monkeypatch, tmp_path: Path):
    stub_fetch(monkeypatch)
    monkeypatch.setattr(download, "_audio_codec", lambda _p: "aac")

    async def fake_ffmpeg(args, *, what, hint):
        Path(args[-1]).write_bytes(b"x")

    monkeypatch.setattr(download, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(download, "require_ffmpeg", lambda: "/fake/ffmpeg")

    seen: list[download.Progress] = []
    await download.download(dash_media(), out_dir=tmp_path, mode="audio", progress=seen.append)

    stages = {p.stage for p in seen}
    assert {"下载音频流", "转码", "完成"} <= stages
    assert seen[-1].overall == 1.0
    assert all(0.0 <= p.overall <= 1.0 for p in seen)
    assert all(p.overall <= 0.72 for p in seen if p.stage == "下载音频流"), "音频模式的下载阶段不该越过后面的转码区间"