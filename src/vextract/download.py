"""下载与合并：选流 → 落盘 → 必要时 ffmpeg 无损合并。

DASH 平台（B站/YouTube）给的是"纯视频轨 + 纯音频轨"，不合并的产物是**静音视频**，
所以 ffmpeg 是硬依赖：启动即探测，缺失就抛 DependencyMissing（不能静默产出坏文件）。

进度与取消都是"回调注入"：CLI 用它渲染终端进度条，HTTP 任务队列用它推 SSE，
两者共用同一份实现，不各写一套。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .errors import Cancelled, DependencyMissing, DownloadFailed
from .models import MediaInfo, Stream

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 各阶段在总进度里的区间（解析由上层驱动，这里先占位）
STAGE_SPANS: dict[str, tuple[float, float]] = {
    "解析": (0.0, 0.08),
    "下载视频流": (0.08, 0.66),
    "下载音频流": (0.66, 0.92),
    "合并": (0.92, 0.99),
    "完成": (1.0, 1.0),
}


@dataclass
class Progress:
    stage: str
    done: int = 0
    total: int | None = None
    overall: float = 0.0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "overall": round(self.overall, 4),
            "message": self.message,
        }


ProgressCb = Callable[[Progress], None]
CancelCb = Callable[[], bool]


def _overall(stage: str, done: int, total: int | None) -> float:
    start, end = STAGE_SPANS.get(stage, (0.0, 1.0))
    ratio = min(done / total, 1.0) if total and total > 0 else (0.0 if stage.endswith("流") else 0.5)
    return start + (end - start) * ratio


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def require_ffmpeg() -> str:
    path = ffmpeg_path()
    if not path:
        raise DependencyMissing(
            "找不到 ffmpeg",
            hint="macOS: `brew install ffmpeg`；Debian/Ubuntu: `apt install ffmpeg`（B站/YouTube 的 DASH 流必须合并）",
        )
    return path


def rank_of(quality: str, height: int = 0, bitrate: int = 0) -> int:
    """统一排序依据：显式清晰度档位 > 分辨率 > 码率（kbps）。

    bitrate 传的是 bps，内部按 kbps 参与排序，避免高码率 360p 压过低码率 480p。
    """
    labels = (
        ("origin", 9500), ("8K", 9000), ("杜比视界", 8800), ("HDR", 8600), ("4K", 8000),
        ("2K", 7000), ("1080P+", 6600), ("1080P60", 6500), ("1080P", 6400), ("超清", 6400), ("原画", 6300),
        ("720P60", 5500), ("720P", 5400), ("高清", 5400), ("hd", 5000),
        ("480P", 4400), ("标清", 4400), ("360P", 3300), ("流畅", 3300), ("sd", 3300),
        ("audio", 100),
    )
    base = 0
    low = quality.lower()
    for label, score in labels:
        if label.lower() in low:
            base = max(base, score)
    return base * 1_000_000 + min(int(height or 0), 4320) * 1000 + min(int(bitrate or 0) // 1000, 999)


def safe_filename(text: str, *, max_length: int = 80, fallback: str = "video") -> str:
    cleaned = _UNSAFE.sub("_", text).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length].strip()


def pick_streams(
    media: MediaInfo,
    quality: str = "best",
    stream_index: int | None = None,
) -> tuple[Stream, Stream | None]:
    """stream_index 是前端表格里的行号（1-based），优先于 quality 字符串。"""
    ordered = sorted(media.streams, key=lambda s: s.rank, reverse=True)
    video_candidates = [s for s in ordered if not s.audio_only]
    if not video_candidates:
        raise DownloadFailed("没有可下载的视频流", hint="先点一次「解析」看看结果", platform=media.platform)

    if stream_index is not None and 1 <= stream_index <= len(ordered):
        chosen = ordered[stream_index - 1]
        if chosen.audio_only:
            raise DownloadFailed(
                "选中的是纯音频轨，不能作为视频流",
                hint="音频轨会自动跟随所选视频轨；请选择视频行",
                platform=media.platform,
            )
        video = chosen
    elif quality != "best":
        want = quality.rstrip("p").lower()
        matched = [s for s in video_candidates if want in s.quality.lower()]
        video = (matched or video_candidates)[0]
    else:
        video = video_candidates[0]

    audio = media.best_audio() if media.needs_merge else None
    return video, audio


def _emit(progress: ProgressCb | None, stage: str, done: int = 0, total: int | None = None, message: str = "") -> None:
    if progress:
        progress(Progress(stage=stage, done=done, total=total, overall=_overall(stage, done, total), message=message))


async def _fetch(
    stream: Stream,
    dest: Path,
    label: str,
    progress: ProgressCb | None,
    cancel: CancelCb | None,
) -> int:
    total_bytes = 0
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as cli, cli.stream(
            "GET", stream.url, headers=stream.headers or None
        ) as resp:
            if resp.status_code >= 400:
                raise DownloadFailed(
                    f"{label} 下载失败：HTTP {resp.status_code}",
                    hint="直链通常带时效签名，重新解析一次即可",
                )
            declared = resp.headers.get("content-length")
            expected: int | None = None
            if declared and declared.isdigit():
                expected = int(declared)
            elif stream.filesize:
                expected = stream.filesize
            _emit(progress, label, 0, expected)
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(256 * 1024):
                    if cancel and cancel():
                        raise Cancelled("下载已取消", hint="任务已停止，临时文件已清理")
                    fh.write(chunk)
                    total_bytes += len(chunk)
                    _emit(progress, label, total_bytes, expected)
    except httpx.HTTPError as exc:
        raise DownloadFailed(f"{label} 下载中断：{exc}", hint="检查网络/代理后重试") from exc
    return total_bytes


async def _merge(video: Path, audio: Path, dest: Path) -> None:
    ffmpeg = require_ffmpeg()
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-i", str(audio),
        "-c", "copy", "-movflags", "+faststart", str(dest),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise DownloadFailed(
            "ffmpeg 合并失败",
            hint="通常说明码流有损坏；重试一次，仍失败请反馈",
            detail=(stderr or b"").decode(errors="ignore")[-400:],
        )


def plan(media: MediaInfo, *, quality: str = "best", stream_index: int | None = None, filename: str | None = None):
    """预览"这次会下哪两路流、存成什么名"，供前端在点下载前显示。"""
    video, audio = pick_streams(media, quality, stream_index)
    stem = filename or safe_filename(f"{media.author or media.platform}_{media.title}_{media.media_id}")
    return video, audio, stem


async def download(
    media: MediaInfo,
    *,
    out_dir: Path,
    quality: str = "best",
    stream_index: int | None = None,
    filename: str | None = None,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    video, audio, stem = plan(media, quality=quality, stream_index=stream_index, filename=filename)
    ext = video.ext or "mp4"
    if audio is not None:
        require_ffmpeg()
    dest = out_dir / f"{stem}.{ext}"
    if dest.exists():
        dest = out_dir / f"{stem}_{media.media_id}.{ext}"

    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".vx-") as tmp:
        tmpdir = Path(tmp)
        vpath = tmpdir / f"video.{video.ext or 'mp4'}"
        await _fetch(video, vpath, "下载视频流", progress, cancel)
        if audio is None:
            shutil.move(str(vpath), dest)
            _emit(progress, "完成", dest.stat().st_size, dest.stat().st_size, str(dest))
            return dest
        apath = tmpdir / f"audio.{audio.ext or 'm4a'}"
        await _fetch(audio, apath, "下载音频流", progress, cancel)
        _emit(progress, "合并", 0, None, "ffmpeg -c copy")
        await _merge(vpath, apath, dest)
        _emit(progress, "完成", dest.stat().st_size, dest.stat().st_size, str(dest))
    return dest