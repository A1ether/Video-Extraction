"""任务队列：状态机、进度推送、取消、历史落盘。

这一层是前端的主流程，所以测试要覆盖"失败/取消不会卡在运行中"这类边界。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from vextract import download as dl
from vextract import registry, tasks
from vextract.errors import NoMedia
from vextract.models import MediaInfo, Stream
from vextract.tasks import TERMINAL, TaskManager


@pytest.fixture(autouse=True)
def clean_manager(monkeypatch, tmp_path: Path):
    """每个用例一个干净的 manager 与独立的 history 文件。"""
    monkeypatch.setattr(tasks.config.settings, "home", tmp_path)
    monkeypatch.setattr(tasks.config.settings, "out_dir", tmp_path)
    manager = TaskManager()
    monkeypatch.setattr(tasks, "manager", manager)
    yield manager


def fake_media() -> MediaInfo:
    return MediaInfo(
        platform="test",
        media_id="1",
        source_url="https://example/v/1",
        title="标题",
        author="作者",
        streams=[Stream(url="https://cdn/v.mp4", quality="480P", rank=dl.rank_of("480P", 480, 100_000))],
        needs_merge=False,
    )


async def wait_terminal(task, timeout: float = 5.0):
    for _ in range(int(timeout / 0.02)):
        if task.state in TERMINAL:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内结束，当前状态 {task.state}")


@pytest.mark.asyncio
async def test_task_runs_to_done_and_records_size(monkeypatch, tmp_path: Path):
    async def fake_extract(text: str, *, quality: str = "best"):
        return fake_media()

    async def fake_download(
        media, *, out_dir, quality="best", stream_index=None, filename=None, mode=None,
        audio_format="m4a", embed_tags=True, transcribe=False, transcript_format="md",
        asr_model=None, asr_language=None, progress=None, cancel=None, on_artifact=None,
    ):
        if progress:
            progress(dl.Progress(stage="下载视频流", done=5, total=10, overall=0.3))
        target = Path(out_dir) / "out.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 32)
        return target

    monkeypatch.setattr(registry, "extract", fake_extract)
    monkeypatch.setattr(dl, "download", fake_download)

    manager = tasks.manager
    task = manager.create(text="https://example/v/1", out_dir=tmp_path)
    assert task.state == tasks.TaskState.queued
    await wait_terminal(task)

    assert task.state == tasks.TaskState.done
    assert task.size == 32
    assert task.progress["overall"] == 1.0
    assert task.media is not None and task.media.title == "标题"


@pytest.mark.asyncio
async def test_task_error_is_structured(monkeypatch, tmp_path: Path):
    async def fake_extract(text: str, *, quality: str = "best"):
        raise NoMedia("这条是图文", hint="请用视频链接", platform="xhs")

    monkeypatch.setattr(registry, "extract", fake_extract)
    task = tasks.manager.create(text="x", out_dir=tmp_path)
    await wait_terminal(task)

    assert task.state == tasks.TaskState.error
    assert task.error["code"] == "NO_MEDIA"
    assert task.error["hint"] == "请用视频链接"


@pytest.mark.asyncio
async def test_unexpected_exception_does_not_stick_in_running(monkeypatch, tmp_path: Path):
    async def boom(text: str, *, quality: str = "best"):
        raise RuntimeError("意料之外")

    monkeypatch.setattr(registry, "extract", boom)
    task = tasks.manager.create(text="x", out_dir=tmp_path)
    await wait_terminal(task)
    assert task.state == tasks.TaskState.error
    assert "RuntimeError" in task.error["message"]


@pytest.mark.asyncio
async def test_cancel_marks_cancelled(monkeypatch, tmp_path: Path):
    async def fake_extract(text: str, *, quality: str = "best"):
        return fake_media()

    async def slow_download(
        media, *, out_dir, quality="best", stream_index=None, filename=None, mode=None,
        audio_format="m4a", embed_tags=True, transcribe=False, transcript_format="md",
        asr_model=None, asr_language=None, progress=None, cancel=None, on_artifact=None,
    ):
        for _ in range(200):
            if cancel and cancel():
                from vextract.errors import Cancelled

                raise Cancelled("下载已取消")
            await asyncio.sleep(0.01)
        return Path(out_dir) / "never.mp4"

    monkeypatch.setattr(registry, "extract", fake_extract)
    monkeypatch.setattr(dl, "download", slow_download)

    task = tasks.manager.create(text="x", out_dir=tmp_path)
    await asyncio.sleep(0.05)
    tasks.manager.cancel(task.id)
    await wait_terminal(task)
    assert task.state == tasks.TaskState.cancelled
    assert task.cancel_requested is True


@pytest.mark.asyncio
async def test_events_fanout_reaches_subscribers(monkeypatch, tmp_path: Path):
    async def fake_extract(text: str, *, quality: str = "best"):
        return fake_media()

    async def fake_download(
        media, *, out_dir, quality="best", stream_index=None, filename=None, mode=None,
        audio_format="m4a", embed_tags=True, transcribe=False, transcript_format="md",
        asr_model=None, asr_language=None, progress=None, cancel=None, on_artifact=None,
    ):
        if progress:
            progress(dl.Progress(stage="下载视频流", done=1, total=2, overall=0.5))
        target = Path(out_dir) / "out.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        return target

    monkeypatch.setattr(registry, "extract", fake_extract)
    monkeypatch.setattr(dl, "download", fake_download)

    manager = tasks.manager
    queue = manager.subscribe()
    task = manager.create(text="x", out_dir=tmp_path)
    await wait_terminal(task)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events, "应有事件推给订阅者"
    assert any(payload["event"] == "progress" and payload["task"]["progress"]["overall"] > 0 for payload in events)
    manager.unsubscribe(queue)
    assert queue not in manager.listeners


def test_history_is_persisted_and_reloaded(monkeypatch, tmp_path: Path):
    manager = tasks.manager
    task = tasks.Task(id="h1", text="https://example/v/1", out_dir=tmp_path, state=tasks.TaskState.done, _manager=manager)
    task.path = tmp_path / "a.mp4"
    task.size = 123
    manager.tasks[task.id] = task
    manager._persist(task)

    history_file = tmp_path / "history.json"
    assert history_file.exists()
    rows = json.loads(history_file.read_text(encoding="utf-8"))
    assert rows[0]["id"] == task.id and rows[0]["size"] == 123

    fresh = TaskManager()
    fresh.load_history()
    assert any(t.id == task.id and t.state == tasks.TaskState.done for t in fresh.tasks.values())


def test_trim_keeps_recent_when_over_limit(monkeypatch, tmp_path: Path):
    manager = tasks.manager
    for i in range(tasks.MAX_TASKS + 20):
        task = tasks.Task(id=f"t{i}", text="x", out_dir=tmp_path, state=tasks.TaskState.done, created_at=float(i))
        manager.tasks[task.id] = task
    manager._trim()
    assert len(manager.tasks) <= tasks.MAX_TASKS
    assert "t0" not in manager.tasks  # 最老的被丢掉


def test_remove_and_clear_finished(tmp_path: Path):
    manager = tasks.manager
    for i in range(3):
        task = tasks.Task(id=f"t{i}", text="x", out_dir=tmp_path, state=tasks.TaskState.done, _manager=manager)
        manager.tasks[task.id] = task
    assert manager.clear_finished() == 3
    assert manager.tasks == {}
    assert manager.remove("nope") is False

# ---------- 产物模式（音频 / 图集）----------
@pytest.mark.asyncio
async def test_task_passes_mode_options_and_collects_artifacts(monkeypatch, tmp_path: Path):
    """图集任务的产物是目录 + 一串图片：主路径给目录，每张图通过 on_artifact 回传。"""
    seen: dict = {}

    async def fake_extract(text: str, *, quality: str = "best"):
        return fake_media()

    async def fake_download(
        media, *, out_dir, quality="best", stream_index=None, filename=None, mode=None,
        audio_format="m4a", embed_tags=True, transcribe=False, transcript_format="md",
        asr_model=None, asr_language=None, progress=None, cancel=None, on_artifact=None,
    ):
        seen.update(mode=mode, audio_format=audio_format, transcribe=transcribe, transcript_format=transcript_format)
        folder = Path(out_dir) / "图集"
        folder.mkdir(parents=True, exist_ok=True)
        if progress:
            progress(dl.Progress(stage="下载图片", done=1, total=2, overall=0.5, message="第 1/2 张"))
        for i in (1, 2):
            item = folder / f"{i:02d}.jpg"
            item.write_bytes(b"x" * 10)
            if on_artifact:
                on_artifact(item)
        return folder

    monkeypatch.setattr(registry, "extract", fake_extract)
    monkeypatch.setattr(dl, "download", fake_download)

    task = tasks.manager.create(text="x", out_dir=tmp_path, mode="images")
    await wait_terminal(task)

    assert task.state == tasks.TaskState.done
    assert seen == {"mode": "images", "audio_format": "m4a", "transcribe": False, "transcript_format": "md"}
    assert task.path and task.path.is_dir()
    assert [item.name for item in task.artifacts] == ["01.jpg", "02.jpg"]
    assert task.size == 20, "目录任务要按文件总和算体积，不能报目录项的 st_size"


@pytest.mark.asyncio
async def test_transcode_and_transcribe_stages_map_to_processing_state(monkeypatch, tmp_path: Path):
    """转码/转录既不是"下载中"也不是"合并中"：前端要能显示成处理阶段。"""
    states: list[str] = []

    async def fake_extract(text: str, *, quality: str = "best"):
        return fake_media()

    async def fake_download(
        media, *, out_dir, quality="best", stream_index=None, filename=None, mode=None,
        audio_format="m4a", embed_tags=True, transcribe=False, transcript_format="md",
        asr_model=None, asr_language=None, progress=None, cancel=None, on_artifact=None,
    ):
        if progress:
            for stage in ("下载音频流", "转码", "转录"):
                progress(dl.Progress(stage=stage, overall=0.5))
        target = Path(out_dir) / "out.mp3"
        target.write_bytes(b"x" * 8)
        return target

    monkeypatch.setattr(registry, "extract", fake_extract)
    monkeypatch.setattr(dl, "download", fake_download)

    task = tasks.manager.create(text="x", out_dir=tmp_path, mode="audio", audio_format="mp3")
    queue = tasks.manager.subscribe()
    await wait_terminal(task)

    while not queue.empty():
        event = queue.get_nowait()
        if event["event"] == "progress":
            states.append(event["task"]["state"])
    tasks.manager.unsubscribe(queue)

    assert "processing" in states, states
    assert task.state == tasks.TaskState.done


def test_history_keeps_mode_and_artifacts(monkeypatch, tmp_path: Path):
    """重启后历史里仍能看到"这是个图集任务"以及它的图片列表。"""
    manager = tasks.manager
    folder = tmp_path / "图集"
    folder.mkdir()
    task = tasks.Task(
        id="h2", text="x", out_dir=tmp_path, state=tasks.TaskState.done, mode="images",
        path=folder, artifacts=[folder / "01.jpg", folder / "02.jpg"], _manager=manager,
    )
    manager.tasks[task.id] = task
    manager._persist(task)

    fresh = TaskManager()
    fresh.load_history()
    reloaded = fresh.tasks["h2"]
    assert reloaded.mode == "images"
    assert [item.name for item in reloaded.artifacts] == ["01.jpg", "02.jpg"]
    assert reloaded.serialize()["artifacts"][0].endswith("01.jpg")
