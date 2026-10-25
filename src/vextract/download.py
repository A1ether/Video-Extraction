"""下载与转换：选流 → 落盘 → 必要时 ffmpeg 处理。

三种产物模式（mode）：
- **video**：DASH 平台（B站/YouTube）给的是"纯视频轨 + 纯音频轨"，不合并的产物是**静音视频**，
  所以 ffmpeg 是硬依赖：启动即探测，缺失就抛 DependencyMissing（不能静默产出坏文件）。
- **audio**：只留音轨。DASH 平台直接下自带音轨（不重编码）；单文件 mp4 平台（微博/快手/小红书/抖音）
  用 ffmpeg `-vn` 抽。目标是 m4a 且源已是 aac 时走 `-c:a copy`——那是"零重编码"，不是转码。
- **images**：图文作品，按 `01.jpg / 02.jpg …` 落进同名文件夹。

进度与取消都是"回调注入"：CLI 用它渲染终端进度条，HTTP 任务队列用它推 SSE，
两者共用同一份实现，不各写一套。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import config
from .errors import Cancelled, DependencyMissing, DownloadFailed
from .models import Asset, AudioFormat, MediaInfo, Mode, Stream, TranscriptFormat

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 各阶段在总进度里的区间（解析由上层驱动，这里先占位）
STAGE_SPANS: dict[str, tuple[float, float]] = {
    "解析": (0.0, 0.08),
    "下载视频流": (0.08, 0.66),
    "下载音频流": (0.66, 0.92),
    "合并": (0.92, 0.99),
    "完成": (1.0, 1.0),
}

# 音频模式：只下一路（自带音轨或整个 mp4），后面可能还有转码与转录
_AUDIO_SPANS: dict[str, tuple[float, float]] = {
    "下载音频流": (0.08, 0.72),
    "转码": (0.72, 0.86),
    "转录": (0.86, 0.99),
    "完成": (1.0, 1.0),
}

# 图集模式：N 张图的字节进度由调用方压进这个区间
_IMAGE_SPANS: dict[str, tuple[float, float]] = {
    "下载图片": (0.06, 0.99),
    "完成": (1.0, 1.0),
}

_MODE_SPANS: dict[str, dict[str, tuple[float, float]]] = {
    "video": STAGE_SPANS,
    "audio": _AUDIO_SPANS,
    "images": _IMAGE_SPANS,
}

# 目标格式 → 源编码落在哪个集合里时可以不重编码（-c:a copy）
_COPY_READY = {"m4a": {"aac"}, "mp3": {"mp3"}, "flac": {"flac"}}
_AUDIO_EXT = {"m4a": "m4a", "mp3": "mp3", "flac": "flac"}


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
ArtifactCb = Callable[[Path], None]
"""转录/图集这类"主产物之外的文件"通过它回传给任务队列。"""


def _overall(stage: str, done: int, total: int | None, mode: Mode = "video") -> float:
    spans = _MODE_SPANS.get(mode, STAGE_SPANS)
    start, end = spans.get(stage, (0.0, 1.0))
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


def resolve_mode(media: MediaInfo, mode: Mode | None = None, *, quality: str = "best") -> Mode:
    """决定这次产出什么。

    - 显式给了 mode 就用它；
    - `quality == "audio"` 视作音频模式——Quality 里那个 `audio` 档一直存在（models.py），
      以前它会被静默当成"最佳视频"，属于"预留了入口但没接线"；
    - 其余按内容推断：只有图 → 图集，否则视频。
    """
    if mode:
        return mode
    if quality == "audio":
        return "audio"
    return "images" if media.is_image_post else "video"


def _stem(media: MediaInfo, filename: str | None) -> str:
    return filename or safe_filename(f"{media.author or media.platform}_{media.title}_{media.media_id}")


def unique_path(target: Path) -> Path:
    """同名已存在时加 _2 / _3 递进，**不要**再拼 media_id。

    踩过的坑：早先是 `stem_{media_id}.ext`，而自动生成的 stem 本身已经以 media_id 结尾，
    于是第二次下载得到 `…_5357907014320571_5357907014320571`——同一串 ID 出现两遍，
    用户看到只会以为是 bug。这里统一成"保留原名，加序号"。
    """
    if not target.exists():
        return target
    index = 2
    while True:
        candidate = target.with_name(f"{target.stem}_{index}{target.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def pick_streams(
    media: MediaInfo,
    quality: str = "best",
    stream_index: int | None = None,
) -> tuple[Stream, Stream | None]:
    """stream_index 是前端表格里的行号（1-based），优先于 quality 字符串。"""
    ordered = sorted(media.streams, key=lambda s: s.rank, reverse=True)
    video_candidates = [s for s in ordered if not s.audio_only]
    if not video_candidates:
        if media.is_image_post:
            raise DownloadFailed(
                "这条是图文作品，没有视频流",
                hint=f"用图集模式下载这 {len(media.assets)} 张图（HTTP: mode=images；CLI: --mode images）",
                platform=media.platform,
            )
        raise DownloadFailed("没有可下载的视频流", hint="先点一次「解析」看看结果", platform=media.platform)

    if stream_index is not None and 1 <= stream_index <= len(ordered):
        chosen = ordered[stream_index - 1]
        if chosen.audio_only:
            raise DownloadFailed(
                "选中的是纯音频轨，不能作为视频流",
                hint="音频轨会自动跟随所选视频轨；请选择视频行，或用音频模式（mode=audio）",
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


def pick_audio(media: MediaInfo, stream_index: int | None = None) -> tuple[Stream, bool]:
    """音频模式选流 → (要下载的那路流, 是否需要从中抽音轨)。

    优先平台自带的独立音轨（DASH 平台，音质无损、不需要重编码）；
    单文件 mp4 平台没有独立音轨，就下那路视频再 `-vn` 抽。
    显式指定行号时尊重用户选择：选中音轨就是音轨，选中视频就是从该视频抽。
    """
    ordered = sorted(media.streams, key=lambda s: s.rank, reverse=True)
    if stream_index is not None and 1 <= stream_index <= len(ordered):
        chosen = ordered[stream_index - 1]
        return chosen, not chosen.audio_only

    audios = [s for s in ordered if s.audio_only]
    if audios:
        return audios[0], False
    videos = [s for s in ordered if not s.audio_only]
    if not videos:
        raise DownloadFailed(
            "这条作品没有音轨",
            hint="确认链接指向视频/音频作品；若浏览器能播，请反馈该链接",
            platform=media.platform,
        )
    return videos[0], True


def _emit(
    progress: ProgressCb | None,
    stage: str,
    done: int = 0,
    total: int | None = None,
    message: str = "",
    mode: Mode = "video",
) -> None:
    if progress:
        progress(
            Progress(
                stage=stage,
                done=done,
                total=total,
                overall=_overall(stage, done, total, mode),
                message=message,
            )
        )


async def _fetch(
    stream: Stream,
    dest: Path,
    label: str,
    progress: ProgressCb | None,
    cancel: CancelCb | None,
    mode: Mode = "video",
    message: str = "",
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
            _emit(progress, label, 0, expected, message, mode)
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(256 * 1024):
                    if cancel and cancel():
                        raise Cancelled("下载已取消", hint="任务已停止，临时文件已清理")
                    fh.write(chunk)
                    total_bytes += len(chunk)
                    _emit(progress, label, total_bytes, expected, message, mode)
    except httpx.HTTPError as exc:
        raise DownloadFailed(f"{label} 下载中断：{exc}", hint="检查网络/代理后重试") from exc
    return total_bytes


async def _run_ffmpeg(args: list[str], *, what: str, hint: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        args[0], *args[1:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise DownloadFailed(what, hint=hint, detail=(stderr or b"").decode(errors="ignore")[-400:])


async def _merge(video: Path, audio: Path, dest: Path) -> None:
    ffmpeg = require_ffmpeg()
    await _run_ffmpeg(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio),
            "-c", "copy", "-movflags", "+faststart", str(dest),
        ],
        what="ffmpeg 合并失败",
        hint="通常说明码流有损坏；重试一次，仍失败请反馈",
    )


def _audio_codec(path: Path) -> str | None:
    """ffprobe 读第一路音频的编码名（"aac"/"mp3"/"flac"/…）；读不到返回 None。

    只为"能不能 -c:a copy"服务：拿不到答案时按需要转码处理，宁可多转一次也不产出坏文件。
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(  # noqa: S603
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip().lower() or None


def audio_codec_args(path: Path, want: AudioFormat) -> list[str]:
    """目标格式 + 源编码 → ffmpeg 的编解码参数。

    源编码与目标容器兼容时用 `-c:a copy`（不重编码，音质无损、秒级完成）；
    否则才真正转码。`-vn` 永远加上：抽音轨时要去掉视频，对纯音频输入是空操作。
    """
    codec = _audio_codec(path)
    if codec and codec in _COPY_READY[want]:
        return ["-vn", "-c:a", "copy"]
    if want == "m4a":
        return ["-vn", "-c:a", "aac", "-b:a", "192k"]
    if want == "mp3":
        return ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
    return ["-vn", "-c:a", "flac"]


def _tag_args(media: MediaInfo) -> list[str]:
    """把标题/作者/来源写进音频标签——自媒体素材进本地音乐库/播客 App 后还认得出。"""
    args: list[str] = []
    for key, value in (("title", media.title), ("artist", media.author), ("comment", media.source_url)):
        if value:
            args += ["-metadata", f"{key}={value}"]
    return args


def plan(media: MediaInfo, *, quality: str = "best", stream_index: int | None = None, filename: str | None = None):
    """预览"这次会下哪两路流、存成什么名"，供前端在点下载前显示。"""
    video, audio = pick_streams(media, quality, stream_index)
    return video, audio, _stem(media, filename)


def plan_audio(
    media: MediaInfo,
    *,
    stream_index: int | None = None,
    filename: str | None = None,
    audio_format: AudioFormat = "m4a",
) -> tuple[Stream, bool, str]:
    stream, extract = pick_audio(media, stream_index)
    return stream, extract, f"{_stem(media, filename)}.{_AUDIO_EXT[audio_format]}"


async def _download_video(
    media: MediaInfo,
    *,
    out_dir: Path,
    quality: str,
    stream_index: int | None,
    filename: str | None,
    progress: ProgressCb | None,
    cancel: CancelCb | None,
) -> Path:
    video, audio, stem = plan(media, quality=quality, stream_index=stream_index, filename=filename)
    ext = video.ext or "mp4"
    if audio is not None:
        require_ffmpeg()
    dest = unique_path(out_dir / f"{stem}.{ext}")

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


async def _download_audio(
    media: MediaInfo,
    *,
    out_dir: Path,
    stream_index: int | None,
    filename: str | None,
    audio_format: AudioFormat,
    embed_tags: bool,
    progress: ProgressCb | None,
    cancel: CancelCb | None,
) -> Path:
    stream, extract, name = plan_audio(
        media, stream_index=stream_index, filename=filename, audio_format=audio_format
    )
    dest = unique_path(out_dir / name)

    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".vx-") as tmp:
        tmpdir = Path(tmp)
        raw = tmpdir / f"audio.{stream.ext or ('mp4' if extract else 'm4a')}"
        await _fetch(stream, raw, "下载音频流", progress, cancel, "audio")

        # 快速通道：源本身就是目标格式的独立音轨、也不用写标签 → 直接改名，不请 ffmpeg。
        if not extract and not embed_tags and raw.suffix.lstrip(".").lower() == _AUDIO_EXT[audio_format]:
            shutil.move(str(raw), dest)
            _emit(progress, "完成", dest.stat().st_size, dest.stat().st_size, str(dest), "audio")
            return dest

        ffmpeg = require_ffmpeg()
        _emit(progress, "转码", 0, None, f"ffmpeg {'抽音轨 →' if extract else '音轨 →'} {audio_format}", "audio")
        await _run_ffmpeg(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(raw),
                *audio_codec_args(raw, audio_format),
                *(_tag_args(media) if embed_tags else []),
                str(dest),
            ],
            what=f"ffmpeg 转 {audio_format} 失败",
            hint="源音轨可能损坏或是罕见编码；换一个格式（m4a/mp3/flac）再试",
        )
        _emit(progress, "完成", dest.stat().st_size, dest.stat().st_size, str(dest), "audio")
    return dest


async def _download_images(
    media: MediaInfo,
    *,
    out_dir: Path,
    filename: str | None,
    progress: ProgressCb | None,
    cancel: CancelCb | None,
    on_artifact: ArtifactCb | None,
) -> Path:
    stem = _stem(media, filename)
    folder = unique_path(out_dir / stem)
    folder.mkdir(parents=True, exist_ok=True)

    ordered = sorted(media.assets, key=lambda a: (a.index, a.url))
    count = len(ordered)
    for position, asset in enumerate(ordered, start=1):
        dest = folder / f"{position:02d}.{asset.ext.lstrip('.') or 'jpg'}"

        def one(progress_payload: Progress, position=position, dest=dest) -> None:
            # 把"单张图的字节进度"压进整组区间：N 张图的总进度要连续，不能每张都从 0 跳到 100%
            ratio = (progress_payload.done / progress_payload.total) if progress_payload.total else 0.5
            overall = 0.06 + ((position - 1 + min(ratio, 1.0)) / max(count, 1)) * 0.93
            if progress:
                progress(
                    Progress(
                        stage="下载图片",
                        done=position,
                        total=count,
                        overall=overall,
                        message=f"第 {position}/{count} 张",
                    )
                )

        await _fetch(_as_stream(asset), dest, "下载图片", one if progress else None, cancel, "images")
        if on_artifact:
            on_artifact(dest)
    _emit(progress, "完成", count, count, str(folder), "images")
    return folder


def _as_stream(asset: Asset) -> Stream:
    """图集复用 _fetch（它吃 Stream）：把一张图包成"单文件流"。"""
    return Stream(url=asset.url, quality="image", ext=asset.ext, headers=asset.headers)


async def download(
    media: MediaInfo,
    *,
    out_dir: Path,
    quality: str = "best",
    stream_index: int | None = None,
    filename: str | None = None,
    mode: Mode | None = None,
    audio_format: AudioFormat = "m4a",
    embed_tags: bool = True,
    transcribe: bool = False,
    transcript_format: TranscriptFormat = "md",
    asr_model: str | None = None,
    asr_language: str | None = None,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
    on_artifact: ArtifactCb | None = None,
) -> Path:
    """落盘入口。返回"主产物"路径：视频文件 / 音频文件 / 图集目录。

    mode 为 None 时按内容推断（也可用 quality="audio" 走音频模式）。
    `transcribe=True` 会在音频落盘后用本地 ASR 生成文字稿（.md/.txt/.srt），
    它们通过 on_artifact 回传，主产物仍是音频文件。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    resolved = resolve_mode(media, mode, quality=quality)

    if resolved == "images":
        if not media.assets:
            raise DownloadFailed(
                "这条作品没有图片",
                hint="确认链接指向图文作品；视频作品请用视频模式（mode=video）",
                platform=media.platform,
            )
        return await _download_images(
            media, out_dir=out_dir, filename=filename, progress=progress, cancel=cancel, on_artifact=on_artifact
        )

    if resolved == "audio":
        path = await _download_audio(
            media,
            out_dir=out_dir,
            stream_index=stream_index,
            filename=filename,
            audio_format=audio_format,
            embed_tags=embed_tags,
            progress=progress,
            cancel=cancel,
        )
        if transcribe:
            from . import asr  # 延迟导入：没装 ASR 依赖时不该影响普通下载

            model = asr_model or config.settings.asr_model
            language = asr_language or config.settings.asr_language
            _emit(progress, "转录", 0, None, f"本地 ASR（{model or asr.DEFAULT_MODEL}）", "audio")
            written = await asr.transcribe_to_files(
                media,
                path,
                fmt=transcript_format,
                model=model,
                language=language,
            )
            if on_artifact:
                for artifact in written:
                    on_artifact(artifact)
            _emit(progress, "完成", path.stat().st_size, path.stat().st_size, str(path), "audio")
        return path

    return await _download_video(
        media,
        out_dir=out_dir,
        quality=quality,
        stream_index=stream_index,
        filename=filename,
        progress=progress,
        cancel=cancel,
    )