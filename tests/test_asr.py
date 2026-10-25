"""本地转录：后端探测、依赖缺失提示、SRT 时间戳、渲染、与下载链路的衔接。

离线跑：所有后端都是猴子补丁的假实现——真正的模型推理在真机用例里验（见 docs/03）。
这一层的核心风险是"看起来成功了，其实什么也没转"或"没装依赖却静默跳过"，所以断言盯住
产物内容与错误码。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vextract import asr, download
from vextract.errors import DependencyMissing, TranscribeFailed
from vextract.models import MediaInfo, Stream


def transcript(*segments: tuple[float, float, str]) -> asr.Transcript:
    items = [asr.Segment(start=s, end=e, text=t) for s, e, t in segments]
    return asr.Transcript(
        text=" ".join(item.text for item in items),
        segments=items,
        language="zh",
        backend="mlx_whisper",
        model="test-model",
    )


MEDIA = MediaInfo(
    platform="douyin",
    media_id="6961737553342991651",
    source_url="https://www.douyin.com/video/6961737553342991651",
    title="小小水手带你去远航",
    author="杨超越",
    duration=19.73,
    stats={"like": 1000, "comment": 20},
)


# ---------- 后端探测 ----------
def test_is_installed_sees_real_modules_and_missing_ones():
    assert asr.is_installed("json") is True
    assert asr.is_installed("definitely_not_a_real_asr_module_xyz") is False


def test_pick_backend_without_any_backend_points_at_the_optional_extra(monkeypatch):
    monkeypatch.setattr(asr, "is_installed", lambda _name: False)
    with pytest.raises(DependencyMissing) as err:
        asr.pick_backend()
    assert err.value.code == "DEPENDENCY_MISSING"
    assert "extra transcribe" in (err.value.hint or ""), "必须给出可执行的安装命令，不能只说'未安装'"


def test_pick_backend_prefers_mlx_then_faster_then_openai(monkeypatch):
    monkeypatch.setattr(asr, "is_installed", lambda name: name in {"faster_whisper", "whisper"})
    assert asr.pick_backend() == "faster_whisper"
    monkeypatch.setattr(asr, "is_installed", lambda name: name == "whisper")
    assert asr.pick_backend() == "whisper"


def test_pick_backend_rejects_unknown_name(monkeypatch):
    with pytest.raises(DependencyMissing) as err:
        asr.pick_backend("whisperx")
    assert "可选" in (err.value.hint or "")


def test_pick_backend_explicit_but_not_installed(monkeypatch):
    monkeypatch.setattr(asr, "is_installed", lambda _name: False)
    with pytest.raises(DependencyMissing):
        asr.pick_backend("mlx_whisper")


def test_model_defaults_per_backend():
    assert asr.model_for("mlx_whisper") == "mlx-community/whisper-large-v3-turbo"
    assert asr.model_for("faster_whisper") == "large-v3-turbo"
    assert asr.model_for("whisper", "tiny") == "tiny"


def test_describe_reports_status_and_hint(monkeypatch):
    monkeypatch.setattr(asr, "is_installed", lambda _name: False)
    status = asr.describe()
    assert status["available"] == [] and status["backend"] is None
    assert status["hint"] and "transcribe" in status["hint"]

    monkeypatch.setattr(asr, "is_installed", lambda name: name == "mlx_whisper")
    status = asr.describe()
    assert status["backend"] == "mlx_whisper" and status["hint"] is None
    assert status["model"].startswith("mlx-community/")


# ---------- SRT ----------
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00:00,000"),
        (1.25, "00:00:01,250"),
        (61.5, "00:01:01,500"),
        (3661.4999, "01:01:01,500"),
        (-3, "00:00:00,000"),
    ],
)
def test_srt_timestamp(seconds, expected):
    assert asr.srt_timestamp(seconds) == expected


def test_render_srt_numbers_blocks_and_uses_comma():
    text = asr.render_srt(transcript((0.0, 1.5, "第一句"), (1.5, 3.0, "第二句")))
    assert text.splitlines()[:3] == ["1", "00:00:00,000 --> 00:00:01,500", "第一句"]
    assert "2\n00:00:01,500 --> 00:00:03,000\n第二句" in text
    assert "," in text and "." not in text.split("-->")[0], "SRT 用逗号做毫秒分隔符"


def test_render_text_is_plain_prose():
    assert asr.render_text(transcript((0, 1, "一句话"))).strip() == "一句话"


def test_render_markdown_carries_metadata_for_writing():
    text = asr.render_markdown(MEDIA, transcript((0, 1, "正文第一句")))
    assert text.startswith("# 小小水手带你去远航")
    assert "@杨超越" in text and "https://www.douyin.com/video/6961737553342991651" in text
    assert "0:19" in text, "时长要人类可读"
    assert "点赞 1000" in text
    assert "mlx_whisper（test-model）" in text
    assert "## 正文" in text and "正文第一句" in text


# ---------- 转录 ----------
def fake_runner(monkeypatch, *, text: str = "一句话", raises: Exception | None = None):
    def runner(_audio: Path, *, model: str, language: str | None) -> asr.Transcript:
        if raises:
            raise raises
        return asr.Transcript(text=text, segments=[asr.Segment(0, 1, text)], language=language, backend="mlx_whisper", model=model)

    monkeypatch.setattr(asr, "is_installed", lambda name: name == "mlx_whisper")
    monkeypatch.setattr(asr, "_RUNNERS", {"mlx_whisper": runner})
    return runner


def test_transcribe_uses_first_available_backend(monkeypatch, tmp_path: Path):
    fake_runner(monkeypatch, text="你好世界")
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    result = asr.transcribe(audio)
    assert result.text == "你好世界"
    assert result.backend == "mlx_whisper" and result.model.startswith("mlx-community/")
    assert result.duration == 1.0


def test_transcribe_missing_file_reports_clearly(tmp_path: Path):
    with pytest.raises(TranscribeFailed) as err:
        asr.transcribe(tmp_path / "nope.m4a")
    assert err.value.code == "TRANSCRIBE_FAILED"


def test_transcribe_empty_result_is_an_error_not_success(monkeypatch, tmp_path: Path):
    """纯音乐/无人声时不能假装转出空文件。"""
    fake_runner(monkeypatch, text="")
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    with pytest.raises(TranscribeFailed) as err:
        asr.transcribe(audio)
    assert "空" in err.value.message


def test_transcribe_translates_backend_crash(monkeypatch, tmp_path: Path):
    fake_runner(monkeypatch, raises=RuntimeError("model not found"))
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    with pytest.raises(TranscribeFailed) as err:
        asr.transcribe(audio)
    assert "RuntimeError" in err.value.message
    assert "faster_whisper" in err.value.message or "mlx_whisper" in err.value.message, "报错要说清试的是哪套后端/模型"
    hint = err.value.hint or ""
    assert "hf-mirror" in hint, "权重下不来是国内最常见的坑，hint 必须提到镜像"
    assert "后端专属" in hint, "模型名与后端绑定的坑要写出来（mlx 全名 vs CT2 短名）"


def test_transcribe_honours_explicit_backend(monkeypatch, tmp_path: Path):
    """非人声音频要能换到 faster_whisper（VAD）；错误提示会推荐它，入口就必须真的存在。"""
    used: list[str] = []

    def mlx(_audio, *, model, language):
        used.append("mlx")
        return asr.Transcript(text="x", backend="mlx_whisper", model=model)

    def faster(_audio, *, model, language):
        used.append("faster")
        return asr.Transcript(text="y", backend="faster_whisper", model=model)

    monkeypatch.setattr(asr, "is_installed", lambda _name: True)
    monkeypatch.setattr(asr, "_RUNNERS", {"mlx_whisper": mlx, "faster_whisper": faster, "whisper": mlx})
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    assert asr.transcribe(audio, backend="faster_whisper").backend == "faster_whisper"
    assert asr.transcribe(audio, backend="mlx_whisper").backend == "mlx_whisper"
    assert used == ["faster", "mlx"], "显式指定的后端必须落到对应 runner"


def test_degeneracy_hint_only_names_backends_the_cli_accepts(monkeypatch, tmp_path: Path):
    """hint 会写「换 --asr-backend faster_whisper 重试」——那个值必须在 BACKENDS 里，
    否则用户照着做只会撞上「不认识的后端」。"""
    fake_runner(monkeypatch, text=HALLUCINATION_LOOP)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    with pytest.raises(TranscribeFailed) as err:
        asr.transcribe(audio)

    hint = err.value.hint or ""
    assert [name for name in asr.BACKENDS if name in hint], hint
    assert "--asr-backend" in hint, hint


def test_av_incompatibility_is_explained_not_dumped(monkeypatch, tmp_path: Path):
    """实测踩到：faster-whisper 1.2.1 传 metadata_errors=，av 19 删了这个参数。
    用户需要看到"装 av<19"，而不是一句 TypeError。"""
    fake_runner(monkeypatch, raises=TypeError("open() got an unexpected keyword argument 'metadata_errors'"))
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    with pytest.raises(TranscribeFailed) as err:
        asr.transcribe(audio)
    assert "av 版本" in err.value.message
    assert "av<19" in (err.value.hint or "")


def test_transcribe_passes_language_and_model_through(monkeypatch, tmp_path: Path):
    seen: dict = {}

    def runner(_audio, *, model, language):
        seen.update(model=model, language=language)
        return asr.Transcript(text="x", backend="mlx_whisper", model=model)

    monkeypatch.setattr(asr, "is_installed", lambda name: name == "mlx_whisper")
    monkeypatch.setattr(asr, "_RUNNERS", {"mlx_whisper": runner})
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    asr.transcribe(audio, model="mlx-community/whisper-small-mlx", language="zh")
    assert seen == {"model": "mlx-community/whisper-small-mlx", "language": "zh"}


@pytest.mark.asyncio
async def test_transcribe_to_files_writes_transcript_next_to_audio(monkeypatch, tmp_path: Path):
    fake_runner(monkeypatch, text="转出来的文字")
    audio = tmp_path / "作者_标题_id.m4a"
    audio.write_bytes(b"x")

    written = await asr.transcribe_to_files(MEDIA, audio, fmt="md")
    assert len(written) == 1
    dest = written[0]
    assert dest.name == "作者_标题_id.md" and dest.parent == audio.parent
    body = dest.read_text(encoding="utf-8")
    assert "转出来的文字" in body and body.startswith("# 小小水手带你去远航")


@pytest.mark.asyncio
async def test_transcribe_to_files_does_not_clobber_existing(monkeypatch, tmp_path: Path):
    fake_runner(monkeypatch, text="新文字")
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    (tmp_path / "a.md").write_text("用户手改过的旧内容", encoding="utf-8")

    written = await asr.transcribe_to_files(MEDIA, audio, fmt="md")
    assert written[0].name == "a_2.md", "冲突时加序号，而不是把 media_id 再拼一遍"
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "用户手改过的旧内容"


@pytest.mark.asyncio
async def test_transcribe_to_files_supports_srt(monkeypatch, tmp_path: Path):
    fake_runner(monkeypatch, text="字幕")
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    written = await asr.transcribe_to_files(MEDIA, audio, fmt="srt")
    assert written[0].suffix == ".srt"
    assert "-->" in written[0].read_text(encoding="utf-8")


# ---------- 重复幻觉（实测踩到过）----------
HALLUCINATION_LOOP = "詞曲 李宗盛 曲 李宗盛 " * 40
NORMAL_SPEECH = (
    "大家好，今天聊三件事：第一，为什么素材要留在本地；第二，有了文字稿为什么写得更快；"
    "第三，平台改版的时候工具该怎么自保。先说第一件——素材留在本地的好处是不受平台寿命影响，"
    "即使某天作品下架，你手里的文件仍然在；第二件的好处是把「听一遍」变成「扫一眼」。"
)


def test_degeneracy_ratio_is_bounded():
    for text in (HALLUCINATION_LOOP, NORMAL_SPEECH, "短句"):
        assert 0.0 <= asr.degeneracy_ratio(text) <= 1.0


def test_repetition_loop_is_flagged():
    assert asr.looks_degenerate(HALLUCINATION_LOOP) is True


def test_normal_speech_is_not_flagged():
    assert asr.looks_degenerate(NORMAL_SPEECH) is False


def test_short_repeats_are_not_flagged():
    """一句「好 好 好」不该被当成幻觉——阈值只对长文本生效。"""
    assert asr.looks_degenerate("好 好 好 好 好") is False


def test_transcribe_rejects_repetition_instead_of_shipping_garbage(monkeypatch, tmp_path: Path):
    """本机实测过：一首歌被写成同一句循环数百次。必须报错，而不是静默给一份"文字稿"。"""
    fake_runner(monkeypatch, text=HALLUCINATION_LOOP)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    with pytest.raises(TranscribeFailed) as err:
        asr.transcribe(audio)
    assert "重复" in err.value.message
    hint = err.value.hint or ""
    assert "人声" in hint
    assert "大概没有人声" not in hint, "提示不该替用户断定原因：实测有'有人声但模型塌了'的情况"
    assert "两种可能" in hint, "要把'没人声'与'模型塌了'并列，否则会误导演用户删素材"
    assert err.value.detail and err.value.detail.startswith("詞曲")


def test_decode_options_turn_off_previous_text_conditioning():
    assert asr.DECODE_OPTIONS["condition_on_previous_text"] is False, "这是重复循环的头号原因"
    assert asr.DECODE_OPTIONS["compression_ratio_threshold"] > 0


def test_mlx_backend_passes_anti_loop_options(monkeypatch, tmp_path: Path):
    import sys
    import types

    captured: dict = {}
    fake = types.ModuleType("mlx_whisper")

    def transcribe(path, *, path_or_hf_repo=None, language=None, **kwargs):
        captured.update(kwargs, model=path_or_hf_repo, language=language)
        return {"segments": [{"start": 0, "end": 1, "text": "你好"}], "language": "zh"}

    fake.transcribe = transcribe  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    result = asr.run_mlx_whisper(audio, model="mlx-community/x", language="zh")

    assert captured["condition_on_previous_text"] is False
    assert captured["model"] == "mlx-community/x"
    assert result.text == "你好" and result.backend == "mlx_whisper"


def test_faster_whisper_backend_enables_vad(monkeypatch, tmp_path: Path):
    """faster-whisper 自带 VAD：直接剔掉无人声段，比事后判重复更早止损。"""
    import sys
    import types

    captured: dict = {}

    class FakeSegment:
        start, end, text = 0.0, 1.0, "你好"

    class FakeInfo:
        language = "zh"

    class FakeModel:
        def __init__(self, name, device=None, compute_type=None):
            captured["model"] = name

        def transcribe(self, _path, **kwargs):
            captured.update(kwargs)
            return iter([FakeSegment()]), FakeInfo()

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    result = asr.run_faster_whisper(audio, model="small", language="zh")

    assert captured["vad_filter"] is True and captured["condition_on_previous_text"] is False
    assert result.text == "你好" and result.language == "zh"


# ---------- 局部退化（逐段判重）----------
def test_segment_loop_detected_for_repeated_char_run():
    """实测残留：真实剧情里出现过「那我會把那套套套套套套套套」。"""
    assert asr.looks_degenerate_segment("那我會把那套套套套套套套套套") is True


def test_segment_loop_detected_for_short_phrase_repeat():
    assert asr.looks_degenerate_segment("你不太好你不太好你不太好") is True


@pytest.mark.parametrize(
    "text",
    [
        "大家好，今天我們來聊聊自媒體素材怎麼整理",
        "对对对",
        "哈哈哈哈哈哈",          # 6 字笑声
        "哈哈哈哈哈哈哈哈",      # 8 字笑声：仍算正常，不标
        "好，那我們下一段再見",
    ],
)
def test_normal_segments_are_not_flagged(text):
    assert asr.looks_degenerate_segment(text) is False


def test_suspect_segments_returns_indices():
    segments = [
        asr.Segment(0, 3, "第一句是正常的话，我们继续往下讲"),
        asr.Segment(3, 6, "那我會把那套套套套套套套套套"),
        asr.Segment(6, 9, "第三句也正常，讲的是别的内容"),
    ]
    assert asr.suspect_segments(segments) == [1]


def test_transcribe_records_suspects_without_failing(monkeypatch, tmp_path: Path):
    """整段没塌（不报错），但局部有循环时要标出来——这是 6:48 那条实测的形状。"""
    def runner(_audio, *, model, language):
        return asr.Transcript(
            text="第一句正常，第二句开始循环，那我會把那套套套套套套套套套，最后一句也正常，收尾了。",
            segments=[
                asr.Segment(0, 4, "第一句正常，第二句开始循环"),
                asr.Segment(4, 8, "那我會把那套套套套套套套套套"),
                asr.Segment(8, 12, "最后一句也正常，收尾了"),
            ],
            language=language,
            backend="faster_whisper",
            model=model,
        )

    monkeypatch.setattr(asr, "is_installed", lambda name: name == "faster_whisper")
    monkeypatch.setattr(asr, "_RUNNERS", {"faster_whisper": runner})
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    result = asr.transcribe(audio, backend="faster_whisper")
    assert result.suspects == [1]


def test_markdown_lists_suspect_segments_with_timestamps():
    transcript = asr.Transcript(
        text="正常一句。那我會把那套套套套套套套套套。",
        segments=[asr.Segment(0, 5, "正常一句"), asr.Segment(65, 70, "那我會把那套套套套套套套套套")],
        backend="faster_whisper",
        model="small",
        suspects=[1],
    )
    body = asr.render_markdown(MEDIA, transcript)
    assert "疑似退化段：1 段" in body
    assert "## 疑似退化段" in body
    assert "`1:05–1:10`" in body, "要带时间戳，方便回原声核对"
    assert "那我會把那套" in body


def test_markdown_has_no_suspect_section_when_clean():
    body = asr.render_markdown(MEDIA, transcript((0, 3, "一句正常的话")))
    assert "疑似退化段" not in body


def test_srt_is_not_polluted_by_suspect_marking():
    """字幕文件要能直接烧进视频，不能因为标注多出奇怪字符。"""
    transcript = asr.Transcript(
        text="那我會把那套套套套套套套套套",
        segments=[asr.Segment(0, 2, "那我會把那套套套套套套套套套")],
        suspects=[0],
    )
    srt = asr.render_srt(transcript)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:02,000\n那我會把那套")
    assert "疑似" not in srt and "⚠" not in srt


# ---------- 与下载链路衔接 ----------
def audio_media() -> MediaInfo:
    return MediaInfo(
        platform="douyin",
        media_id="6961737553342991651",
        source_url="https://www.douyin.com/video/6961737553342991651",
        title="标题",
        author="作者",
        streams=[Stream(url="https://cdn/v.mp4", quality="1080P", rank=download.rank_of("1080P", 1080, 1_000_000))],
        needs_merge=False,
    )


@pytest.mark.asyncio
async def test_download_transcribes_after_audio_and_reports_artifacts(monkeypatch, tmp_path: Path):
    """音频落盘 → 本地转录 → 文字稿通过 on_artifact 回传；主产物仍是音频。"""
    transcript_file = tmp_path / "作者_标题_6961737553342991651.md"

    async def fake_transcribe(media, audio, *, fmt="md", backend=None, model=None, language=None):
        assert audio.suffix == ".m4a" and audio.exists()
        assert fmt == "md"
        transcript_file.write_text("# 文字稿", encoding="utf-8")
        return [transcript_file]

    async def fake_fetch(stream, dest, label, progress, cancel=None, mode="video", message=""):
        dest.write_bytes(b"audio-bytes")
        return 11

    async def fake_ffmpeg(args, *, what, hint):
        Path(args[-1]).write_bytes(b"m4a")   # 抽音轨这一步的产物

    monkeypatch.setattr(download, "_fetch", fake_fetch)
    monkeypatch.setattr(download, "_audio_codec", lambda _p: "aac")
    monkeypatch.setattr(download, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(download, "require_ffmpeg", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(asr, "transcribe_to_files", fake_transcribe)

    artifacts: list[Path] = []
    seen: list[download.Progress] = []
    path = await download.download(
        audio_media(), out_dir=tmp_path, mode="audio", embed_tags=False,
        transcribe=True, progress=seen.append, on_artifact=artifacts.append,
    )

    assert path.suffix == ".m4a" and path.exists()
    assert artifacts == [transcript_file]
    stages = [p.stage for p in seen]
    assert "转录" in stages and stages[-1] == "完成"
    assert any("mlx-community" in p.message for p in seen if p.stage == "转录"), "转录阶段要写明用哪个模型"


@pytest.mark.asyncio
async def test_download_without_asr_dependency_fails_loudly(monkeypatch, tmp_path: Path):
    """没装 ASR 依赖时宁可报 DEPENDENCY_MISSING，也不能静默只给音频。"""
    async def fake_fetch(stream, dest, label, progress, cancel=None, mode="video", message=""):
        dest.write_bytes(b"audio-bytes")
        return 11

    async def fake_ffmpeg(args, *, what, hint):
        Path(args[-1]).write_bytes(b"m4a")

    monkeypatch.setattr(download, "_fetch", fake_fetch)
    monkeypatch.setattr(download, "_audio_codec", lambda _p: "aac")
    monkeypatch.setattr(download, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(download, "require_ffmpeg", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(asr, "is_installed", lambda _name: False)

    with pytest.raises(DependencyMissing):
        await download.download(
            audio_media(), out_dir=tmp_path, mode="audio", embed_tags=False, transcribe=True
        )