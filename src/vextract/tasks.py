"""下载任务队列：前端看到的进度、状态、历史都由这里产出。

设计取舍：
- 任务是进程内 asyncio 任务 + 队列广播（SSE 推送），不做分布式队列 —— 这是本机工具。
- 历史落盘到 ~/.config/vextract/history.json（只存元数据），重启后仍能看到"下载过什么"。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import config, download, registry
from .errors import Cancelled, VextractError
from .models import MediaInfo

MAX_TASKS = 200


class TaskState(StrEnum):
    queued = "queued"
    resolving = "resolving"
    downloading = "downloading"
    merging = "merging"
    done = "done"
    error = "error"
    cancelled = "cancelled"


TERMINAL = {TaskState.done, TaskState.error, TaskState.cancelled}


@dataclass
class Task:
    id: str
    text: str
    out_dir: Path
    quality: str = "best"
    stream_index: int | None = None
    filename: str | None = None
    state: TaskState = TaskState.queued
    progress: dict = field(default_factory=lambda: download.Progress(stage="排队").to_dict())
    media: MediaInfo | None = None
    path: Path | None = None
    size: int | None = None
    error: dict | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_requested: bool = False
    _manager: TaskManager | None = field(default=None, repr=False)

    def serialize(self) -> dict:
        return {
            "id": self.id,
            "text": self.text[:200],
            "state": self.state.value,
            "progress": self.progress,
            "quality": self.quality,
            "stream_index": self.stream_index,
            "filename": self.filename,
            "out_dir": str(self.out_dir),
            "media": self.media.model_dump() if self.media else None,
            "path": str(self.path) if self.path else None,
            "size": self.size,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.created_at, 1),
        }

    def publish(self, event: str = "progress") -> None:
        if self._manager:
            self._manager.fanout({"event": event, "task": self.serialize()})


class TaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self._jobs: dict[str, asyncio.Task] = {}
        self.listeners: list[asyncio.Queue] = []
        self._history_file = config.settings.home / "history.json"

    def fanout(self, payload: dict) -> None:
        """把任务事件推给所有 SSE 订阅者；堵住的订阅者丢帧，不拖慢下载。"""
        for queue in list(self.listeners):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    # --- 历史 ---
    def load_history(self) -> None:
        if not self._history_file.exists():
            return
        try:
            rows = json.loads(self._history_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for row in rows if isinstance(rows, list) else []:
            task = Task(
                id=row.get("id") or uuid.uuid4().hex[:12],
                text=row.get("text") or "",
                out_dir=Path(row.get("out_dir") or config.settings.out_dir),
                state=TaskState(row.get("state") or "done"),
                path=Path(row["path"]) if row.get("path") else None,
                size=row.get("size"),
                error=row.get("error"),
                created_at=row.get("created_at") or time.time(),
                finished_at=row.get("finished_at"),
                progress={
                    "stage": "完成" if row.get("state") == "done" else "历史",
                    "overall": 1.0,
                    "done": 0,
                    "total": None,
                    "message": "",
                },
                _manager=self,
            )
            self.tasks[task.id] = task

    def _persist(self, task: Task) -> None:
        rows: list = []
        if self._history_file.exists():
            try:
                rows = json.loads(self._history_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                rows = []
        if not isinstance(rows, list):
            rows = []
        rows = [row for row in rows if row.get("id") != task.id]
        rows.append(
            {
                "id": task.id,
                "text": task.text[:200],
                "out_dir": str(task.out_dir),
                "state": task.state.value,
                "path": str(task.path) if task.path else None,
                "size": task.size,
                "error": task.error,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
            }
        )
        config.settings.home.mkdir(parents=True, exist_ok=True)
        self._history_file.write_text(json.dumps(rows[-MAX_TASKS:], ensure_ascii=False, indent=1), encoding="utf-8")

    # --- 生命周期 ---
    def create(
        self,
        *,
        text: str,
        out_dir: Path | None = None,
        quality: str = "best",
        stream_index: int | None = None,
        filename: str | None = None,
    ) -> Task:
        task = Task(
            id=uuid.uuid4().hex[:12],
            text=text,
            out_dir=out_dir or config.settings.out_dir,
            quality=quality,
            stream_index=stream_index,
            filename=filename,
            _manager=self,
        )
        self.tasks[task.id] = task
        self._trim()
        job = asyncio.create_task(self._run(task))
        self._jobs[task.id] = job
        job.add_done_callback(lambda _t: self._jobs.pop(task.id, None))
        return task

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def snapshot(self) -> list[dict]:
        rows = sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)
        return [t.serialize() for t in rows]

    def cancel(self, task_id: str) -> Task | None:
        task = self.tasks.get(task_id)
        if not task or task.state in TERMINAL:
            return task
        task.cancel_requested = True
        job = self._jobs.get(task_id)
        if job:
            job.cancel()
        task.state = TaskState.cancelled
        task.finished_at = time.time()
        task.progress = download.Progress(stage="已取消").to_dict()
        task.publish("state")
        self._persist(task)
        return task

    def remove(self, task_id: str) -> bool:
        task = self.tasks.pop(task_id, None)
        if not task:
            return False
        self._jobs.pop(task_id, None)
        self.publish_history()
        return True

    def clear_finished(self) -> int:
        removed = [tid for tid, t in self.tasks.items() if t.state in TERMINAL]
        for tid in removed:
            self.tasks.pop(tid, None)
        self.publish_history()
        return len(removed)

    def publish_history(self) -> None:
        for task in self.tasks.values():
            task.publish("state")

    # --- 订阅（SSE） ---
    def subscribe(self) -> asyncio.Queue:
        """一个 SSE 连接一个队列；事件里带 task，前端自己按 id 过滤。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        self.listeners.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self.listeners:
            self.listeners.remove(queue)

    # --- 实际执行 ---
    async def _run(self, task: Task) -> None:
        def progress_cb(progress: download.Progress) -> None:
            task.progress = progress.to_dict()
            if progress.stage.startswith("下载"):
                task.state = TaskState.downloading
            elif progress.stage == "合并":
                task.state = TaskState.merging
            task.publish("progress")

        try:
            task.state = TaskState.resolving
            task.progress = download.Progress(stage="解析", overall=0.02).to_dict()
            task.publish("state")
            media = await registry.extract(task.text)
            task.media = media

            task.state = TaskState.downloading
            task.publish("state")
            path = await download.download(
                media,
                out_dir=task.out_dir,
                quality=task.quality,
                stream_index=task.stream_index,
                filename=task.filename,
                progress=progress_cb,
                cancel=lambda: task.cancel_requested,
            )
            task.path = path
            task.size = path.stat().st_size
            task.state = TaskState.done
            task.progress = download.Progress(stage="完成", overall=1.0, message=str(path)).to_dict()
        except Cancelled as exc:
            task.state = TaskState.cancelled
            task.error = exc.to_dict()
            task.progress = download.Progress(stage="已取消").to_dict()
        except VextractError as exc:
            task.state = TaskState.error
            task.error = exc.to_dict()
            task.progress = download.Progress(stage="失败", message=exc.message).to_dict()
        except asyncio.CancelledError:
            task.state = TaskState.cancelled
            task.progress = download.Progress(stage="已取消").to_dict()
        except Exception as exc:  # noqa: BLE001 — 兜底，保证任务不会卡在运行中
            task.state = TaskState.error
            task.error = {"code": "UNEXPECTED", "message": f"{type(exc).__name__}: {exc}"}
            task.progress = download.Progress(stage="失败", message=str(exc)).to_dict()
        finally:
            task.finished_at = time.time()
            task.publish("done" if task.state == TaskState.done else "state")
            self._persist(task)

    def _trim(self) -> None:
        if len(self.tasks) <= MAX_TASKS:
            return
        finished = sorted(
            (t for t in self.tasks.values() if t.state in TERMINAL),
            key=lambda t: t.created_at,
        )
        for task in finished[: len(self.tasks) - MAX_TASKS]:
            self.tasks.pop(task.id, None)


manager = TaskManager()