"""本地转录（ASR）：音频 → 文字稿，与平台登录态无关。

为什么值得有这一层：国内平台的 CC 字幕是**登录态产物**（实测：B站匿名
`need_login_subtitle=True`、`subtitles=[]`；抖音 web 详情里根本没有字幕轨），
所以"要字幕"这件事不能挂在平台上——本地 ASR 完全离线、与平台改版无关，
而且它产出的 `.srt` 还能反过来内嵌回视频。

后端可插拔（按可用性自动挑）：`mlx_whisper`（Apple Silicon，最快）→ `faster_whisper`
（跨平台）→ `whisper`（OpenAI 参考实现）。三者都是 MIT/Unlicense 系许可，符合本项目的依赖口径。

依赖是**可选**的：没装就抛 DependencyMissing 并给出可执行的安装命令，
绝不静默跳过（与 ffmpeg 的处理一致）。模型权重首次使用会从 HuggingFace 下载，
`HF_ENDPOINT=https://hf-mirror.com` 可走国内镜像。
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .errors import DependencyMissing, TranscribeFailed
from .models import MediaInfo, TranscriptFormat

BACKENDS = ("mlx_whisper", "faster_whisper", "whisper")
"""按偏好排序：Apple Silicon 上用 mlx 最快，faster_whisper 次之，openai whisper 兜底。"""

DEFAULT_MODELS: dict[str, str] = {
    "mlx_whisper": "mlx-community/whisper-large-v3-turbo",
    "faster_whisper": "large-v3-turbo",
    "whisper": "small",
}
DEFAULT_MODEL = DEFAULT_MODELS["mlx_whisper"]
"""首选后端的默认模型（`vx doctor` / 前端显示用）。large-v3-turbo 约 1.6GB。"""

INSTALL_HINT = (
    "本地转录需要装一个可选依赖（二选一）：\n"
    "  uv sync --extra transcribe        # 跨平台、体积小（faster-whisper / CTranslate2）\n"
    "  uv sync --extra transcribe-mlx    # Apple Silicon 最快；mlx-whisper 连带 torch，实测 .venv 约 1.3GB\n"
    "模型权重首次使用会下载（默认 large-v3-turbo 约 1.6GB）；国内可 export HF_ENDPOINT=https://hf-mirror.com"
)


DECODE_OPTIONS: dict[str, Any] = {
    # 关掉"用前文当提示"——这是 Whisper 在音乐/静音上陷入重复循环的头号原因
    # （实测：一首歌被写成「詞曲 李宗盛 曲 李宗盛 … Zither Harp …」循环数百次）。
    "condition_on_previous_text": False,
    # 压缩比过高 = 输出自我重复，交给解码器自己重试
    "compression_ratio_threshold": 2.4,
    "no_speech_threshold": 0.6,
}

_DEGENERATE_MIN_CHARS = 120
"""短文本不判重复：一句"好 好 好"不该被当成幻觉。"""


def degeneracy_ratio(text: str) -> float:
    """重复度 = 1 − 唯一 4-gram 占比（0~1，越大越像循环）。

    中文没有词边界，所以不看词、看 4 字符片段：正常口语里几乎每个片段都是新出现的
    （重复度 ≈ 0.05），周期性循环输出则只有寥寥几种片段反复出现（重复度 → 0.97）。

    注意别用"最高频片段的占比"当判据——周期串里每种片段出现次数一样多，那个指标反而很小。
    """
    compact = re.sub(r"\s+", "", text)
    if len(compact) < _DEGENERATE_MIN_CHARS:
        return 0.0
    grams = [compact[i : i + 4] for i in range(len(compact) - 3)]
    if not grams:
        return 0.0
    return 1.0 - len(set(grams)) / len(grams)


def looks_degenerate(text: str) -> bool:
    """像"重复幻觉"吗？（同一小段内容被循环输出）

    宁可把它当错误报出来，也不要静默给用户一份看着像文字稿、其实是循环垃圾的文件——
    这正是 Whisper 在非人声音频（纯音乐/环境音）上的典型失败模式（本机实测已复现）。
    """
    return degeneracy_ratio(text) >= 0.4


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    text: str
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    backend: str = ""
    model: str = ""

    @property
    def duration(self) -> float | None:
        return self.segments[-1].end if self.segments else None


class BackendFn(Protocol):
    def __call__(self, audio: Path, *, model: str, language: str | None) -> Transcript: ...


# ---------- 后端探测 ----------
def is_installed(name: str) -> bool:
    """只探模块在不在（不 import）：/doctor 每次刷新都跑，不能顺带加载 1GB 的模型库。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def available_backends() -> list[str]:
    return [name for name in BACKENDS if is_installed(name)]


def pick_backend(name: str | None = None) -> str:
    """显式指定就校验它；否则按偏好挑第一个装了的。"""
    if name:
        if name not in BACKENDS:
            raise DependencyMissing(
                f"不认识的 ASR 后端：{name}",
                hint=f"可选：{'、'.join(BACKENDS)}；留空则自动挑",
            )
        if not is_installed(name):
            raise DependencyMissing(f"ASR 后端 {name} 没有安装", hint=INSTALL_HINT)
        return name
    found = available_backends()
    if not found:
        raise DependencyMissing("本地没有任何可用的 ASR 后端", hint=INSTALL_HINT)
    return found[0]


def model_for(backend: str, model: str | None = None) -> str:
    return model or DEFAULT_MODELS.get(backend, DEFAULT_MODEL)


def describe() -> dict[str, Any]:
    """给 /doctor 与 `vx doctor` 用的一行状态。"""
    found = available_backends()
    backend = found[0] if found else None
    return {
        "available": found,
        "backend": backend,
        "model": model_for(backend, None) if backend else DEFAULT_MODEL,
        "hint": INSTALL_HINT if not found else None,
    }


# ---------- 后端实现（重量级 import 都关在函数里）----------
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _from_segments(raw: list[dict[str, Any]], backend: str, model: str, language: str | None) -> Transcript:
    segments = [
        Segment(
            start=float(item.get("start") or 0.0),
            end=float(item.get("end") or 0.0),
            text=_clean(str(item.get("text") or "")),
        )
        for item in raw
        if _clean(str(item.get("text") or ""))
    ]
    joined = _clean(" ".join(segment.text for segment in segments))
    return Transcript(text=joined, segments=segments, language=language, backend=backend, model=model)


def run_mlx_whisper(audio: Path, *, model: str, language: str | None) -> Transcript:
    import mlx_whisper  # noqa: PLC0415 — 可选依赖，必须在调用时才 import

    result = mlx_whisper.transcribe(
        str(audio),
        path_or_hf_repo=model,
        language=language,
        **DECODE_OPTIONS,
    )
    return _from_segments(result.get("segments") or [], "mlx_whisper", model, result.get("language") or language)


def run_faster_whisper(audio: Path, *, model: str, language: str | None) -> Transcript:
    from faster_whisper import WhisperModel  # noqa: PLC0415

    engine = WhisperModel(model, device="auto", compute_type="int8")
    # faster-whisper 自带 VAD：能直接把"没有人声的段"剔掉，比 mlx 少一整类幻觉
    segments, info = engine.transcribe(str(audio), language=language, vad_filter=True, **DECODE_OPTIONS)
    raw = [{"start": s.start, "end": s.end, "text": s.text} for s in segments]
    return _from_segments(raw, "faster_whisper", model, getattr(info, "language", None) or language)


def run_openai_whisper(audio: Path, *, model: str, language: str | None) -> Transcript:
    import whisper  # noqa: PLC0415

    result = whisper.load_model(model).transcribe(str(audio), language=language, **DECODE_OPTIONS)
    return _from_segments(result.get("segments") or [], "whisper", model, result.get("language") or language)


_RUNNERS: dict[str, BackendFn] = {
    "mlx_whisper": run_mlx_whisper,
    "faster_whisper": run_faster_whisper,
    "whisper": run_openai_whisper,
}


def transcribe(
    audio: Path,
    *,
    backend: str | None = None,
    model: str | None = None,
    language: str | None = None,
) -> Transcript:
    """同步转录（阻塞；调用方负责放进线程池）。"""
    if not audio.exists():
        raise TranscribeFailed(f"找不到音频文件：{audio}", hint="先确认音频下载成功")
    name = pick_backend(backend)
    resolved = model_for(name, model)
    try:
        result = _RUNNERS[name](audio, model=resolved, language=language)
    except DependencyMissing:
        raise
    except Exception as exc:  # noqa: BLE001 — 模型/权重/设备问题统一翻译
        raise TranscribeFailed(
            f"转录失败（{name}）：{type(exc).__name__}: {exc}",
            hint="常见原因：模型权重没下下来（可 export HF_ENDPOINT=https://hf-mirror.com）、磁盘空间不足、音频损坏",
            detail=str(exc)[:200],
        ) from exc
    if not result.text:
        raise TranscribeFailed(
            "转录结果为空",
            hint="音频里可能没有人声（纯音乐/环境音）；换一条内容再试",
        )
    if looks_degenerate(result.text):
        raise TranscribeFailed(
            "转录结果像是重复幻觉（同一段内容循环出现）",
            hint=(
                "这条音频里大概没有人声（纯音乐/BGM/环境音）——Whisper 在这类音频上会陷入重复循环，"
                "本机实测：一首歌被写成「詞曲 李宗盛 曲 李宗盛 … Zither Harp …」循环数百次。\n"
                "建议：换成有人声的素材；只要音频就 `--mode audio`（不要 --transcribe）；"
                "或改用 faster-whisper 后端（自带 VAD，能剔掉无人声段）"
            ),
            detail=result.text[:120],
        )
    return result


# ---------- 渲染 ----------
def srt_timestamp(seconds: float) -> str:
    """SRT 时间戳：00:00:01,250（逗号是规范要求的，不是笔误）。"""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def render_srt(transcript: Transcript) -> str:
    blocks = []
    for index, segment in enumerate(transcript.segments, start=1):
        blocks.append(
            f"{index}\n{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}\n{segment.text}\n"
        )
    return "\n".join(blocks) if blocks else _clean(transcript.text) + "\n"


def render_text(transcript: Transcript) -> str:
    return transcript.text + "\n"


def render_markdown(media: MediaInfo, transcript: Transcript) -> str:
    """带元信息的文字稿：封面/互动数/链接都在，直接当自媒体写作素材用。"""
    lines = [f"# {media.title}", ""]
    if media.author:
        lines.append(f"- 作者：@{media.author}")
    lines.append(f"- 平台：{media.platform}　链接：{media.source_url}")
    if media.duration:
        lines.append(f"- 时长：{_clock(media.duration)}")
    if media.stats:
        pairs = "、".join(f"{label} {media.stats[key]}" for key, label in _STAT_LABELS if media.stats.get(key))
        if pairs:
            lines.append(f"- 互动：{pairs}")
    lang = f"，语言 {transcript.language}" if transcript.language else ""
    lines.append(f"- 转写：本地 {transcript.backend}（{transcript.model}）{lang}")
    lines += ["", "## 正文", "", transcript.text, ""]
    return "\n".join(lines)


_STAT_LABELS = (("view", "播放"), ("like", "点赞"), ("comment", "评论"), ("favorite", "收藏"), ("repost", "转发"))


def _clock(seconds: float) -> str:
    total = int(seconds)
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


_RENDERERS = {"md": render_markdown, "txt": render_text, "srt": render_srt}


def render(media: MediaInfo, transcript: Transcript, fmt: TranscriptFormat) -> str:
    if fmt == "md":
        return render_markdown(media, transcript)
    if fmt == "srt":
        return render_srt(transcript)
    return render_text(transcript)


async def transcribe_to_files(
    media: MediaInfo,
    audio: Path,
    *,
    fmt: TranscriptFormat = "md",
    backend: str | None = None,
    model: str | None = None,
    language: str | None = None,
) -> list[Path]:
    """转录并落盘（与音频同目录同名，只换扩展名）；返回写出的文件。

    阻塞的模型推理放进线程池，保持调用点 async —— 与 engine 里 yt-dlp 的处理一致。
    """
    transcript = await asyncio.to_thread(
        transcribe, audio, backend=backend, model=model, language=language
    )
    from .download import unique_path  # 本地导入，避免模块级循环依赖

    dest = unique_path(audio.with_suffix(f".{fmt}"))
    dest.write_text(render(media, transcript, fmt), encoding="utf-8")
    return [dest]