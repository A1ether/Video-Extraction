"""选流、命名、ffmpeg 依赖——"静音视频"这类静默错误必须在这一层被挡住。"""

from __future__ import annotations

from pathlib import Path

import pytest

from vextract import download
from vextract.errors import DependencyMissing, DownloadFailed
from vextract.models import MediaInfo, Stream


def make_media(**overrides) -> MediaInfo:
    streams = overrides.pop(
        "streams",
        [
            Stream(url="https://cdn/v360.mp4", quality="360P", rank=download.rank_of("360P", 360, 350_000)),
            Stream(url="https://cdn/v480.mp4", quality="480P", rank=download.rank_of("480P", 480, 200_000)),
            Stream(url="https://cdn/v1080.mp4", quality="1080P", rank=download.rank_of("1080P", 1080, 2_000_000)),
            Stream(url="https://cdn/a128.m4a", quality="audio-128k", audio_only=True, ext="m4a", rank=download.rank_of("audio", bitrate=128_000)),
            Stream(url="https://cdn/a320.m4a", quality="audio-320k", audio_only=True, ext="m4a", rank=download.rank_of("audio", bitrate=320_000)),
        ],
    )
    return MediaInfo(
        platform="test",
        media_id="1",
        source_url="https://example/v/1",
        title="标题",
        author="作者",
        streams=streams,
        needs_merge=overrides.pop("needs_merge", True),
        **overrides,
    )


def test_rank_prefers_explicit_label_over_bitrate():
    # 高码率 360p 不应压过低码率 480p：档位权重高于码率
    assert download.rank_of("480P", 480, 200_000) > download.rank_of("360P", 360, 350_000)
    assert download.rank_of("1080P") > download.rank_of("720P")
    assert download.rank_of("origin") > download.rank_of("8K")


def test_pick_streams_best_is_highest_rank():
    video, audio = download.pick_streams(make_media(), "best")
    assert video.quality == "1080P"
    assert audio is not None and audio.quality == "audio-320k"


def test_pick_streams_quality_filter():
    video, _ = download.pick_streams(make_media(), "480p")
    assert video.quality == "480P"


def test_pick_streams_without_audio_when_no_merge():
    video, audio = download.pick_streams(make_media(needs_merge=False), "best")
    assert video.quality == "1080P"
    assert audio is None


def test_pick_streams_without_video_raises():
    media = make_media(streams=[Stream(url="u", quality="audio", audio_only=True)])
    with pytest.raises(DownloadFailed):
        download.pick_streams(media, "best")


def test_safe_filename_strips_path_and_illegal_chars():
    name = download.safe_filename('a/b:c*d?e"f<g>h|i\nj')
    assert "/" not in name and ":" not in name and '"' not in name
    assert name.startswith("a")


def test_safe_filename_fallback_and_length():
    assert download.safe_filename("   ", fallback="video") == "video"
    assert len(download.safe_filename("x" * 500)) <= 80


def test_require_ffmpeg_reports_missing_dependency(monkeypatch):
    monkeypatch.setattr(download.shutil, "which", lambda _: None)
    with pytest.raises(DependencyMissing) as err:
        download.require_ffmpeg()
    assert err.value.code == "DEPENDENCY_MISSING"
    assert "ffmpeg" in (err.value.hint or "")


@pytest.mark.asyncio
async def test_download_merge_requires_ffmpeg_before_touching_network(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(download.shutil, "which", lambda _: None)
    with pytest.raises(DependencyMissing):
        await download.download(make_media(), out_dir=tmp_path)