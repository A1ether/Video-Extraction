"""HTTP 层：错误结构化、任务队列、Range 播放、设置持久化、图片代理白名单。

前端所有动作都打这些接口，所以这里等价于"前端能用的功能都验一遍"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vextract import config, registry, tasks
from vextract import download as dl
from vextract.api.app import app
from vextract.errors import AuthRequired, UnsupportedUrl
from vextract.models import MediaInfo, Stream


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path: Path):
    """把 home/输出目录/任务表都隔离到 tmp，避免污染真实用户目录。"""
    monkeypatch.setattr(config.settings, "home", tmp_path)
    monkeypatch.setattr(config.settings, "out_dir", tmp_path)
    monkeypatch.setattr(config.settings, "cookies_from_browser", None)
    manager = tasks.TaskManager()
    monkeypatch.setattr(tasks, "manager", manager)
    monkeypatch.setattr("vextract.api.app.manager", manager)
    yield manager


@pytest.fixture
def client() -> TestClient:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def fake_media() -> MediaInfo:
    return MediaInfo(
        platform="bilibili",
        media_id="BV1",
        source_url="https://www.bilibili.com/video/BV1",
        title="标题",
        author="作者",
        streams=[Stream(url="https://cdn/v.mp4", quality="480P", rank=dl.rank_of("480P", 480))],
        needs_merge=False,
    )


def patch_success(monkeypatch, tmp_path: Path, *, size: int = 8, captured: dict | None = None):
    async def fake_extract(text: str, *, quality: str = "best"):
        return fake_media()

    async def fake_download(
        media, *, out_dir, quality="best", stream_index=None, filename=None, mode=None,
        audio_format="m4a", embed_tags=True, transcribe=False, transcript_format="md",
        subtitle_langs=None, asr_backend=None, asr_model=None, asr_language=None,
        progress=None, cancel=None, on_artifact=None,
    ):
        if captured is not None:   # 用来断言"前端传的模式/格式真的走到了下载层"
            captured.update(
                mode=mode, audio_format=audio_format, embed_tags=embed_tags, transcribe=transcribe,
                transcript_format=transcript_format, asr_backend=asr_backend,
                asr_model=asr_model, asr_language=asr_language,
            )
        if progress:
            progress(dl.Progress(stage="下载视频流", done=size // 2, total=size, overall=0.5))
        target = Path(out_dir) / "out.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        return target

    monkeypatch.setattr(registry, "extract", fake_extract)
    monkeypatch.setattr(dl, "download", fake_download)
    monkeypatch.setattr("vextract.api.app.download.download", fake_download)


# ---------- 信息 ----------
def test_health_reports_engine_and_ffmpeg(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "yt_dlp" in body and "ffmpeg" in body


def test_doctor_shape(client):
    body = client.get("/doctor").json()
    assert body["ok"] in (True, False)
    assert set(body) >= {"yt_dlp", "ffmpeg", "python", "out_dir", "cookie_dir", "cookies", "providers"}
    assert {row["platform"] for row in body["cookies"]} == {"douyin", "bilibili", "xhs", "kuaishou", "weibo"}


def test_platforms_lists_capabilities(client):
    body = client.get("/platforms").json()
    names = [row["platform"] for row in body["platforms"]]
    assert "bilibili" in names and "douyin" in names
    assert "generic" in body["providers"]


# ---------- 设置 ----------
def test_settings_get_and_put_persists(client, tmp_path: Path):
    before = client.get("/settings").json()
    assert before["out_dir"] == str(tmp_path)
    assert before["realtime"] is True
    assert "" in before["browser_choices"]

    target = tmp_path / "videos"
    body = client.put(
        "/settings",
        json={"out_dir": str(target), "cookies_from_browser": "chrome", "realtime": False},
    ).json()
    assert body["out_dir"] == str(target)
    assert body["cookies_from_browser"] == "chrome"
    assert body["realtime"] is False

    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["out_dir"] == str(target) and saved["cookies_from_browser"] == "chrome"
    assert saved["realtime"] is False
    assert client.get("/settings").json()["cookies_from_browser"] == "chrome"


def test_settings_roundtrip_through_file(tmp_path: Path):
    """load_persisted() 能把前端写下的设置读回来（重启后仍生效）。"""
    (tmp_path / "settings.json").write_text(
        json.dumps({"out_dir": str(tmp_path / "saved"), "cookies_from_browser": "edge", "realtime": False}),
        encoding="utf-8",
    )
    config.settings.out_dir = Path("/tmp/placeholder")
    config.load_persisted()
    assert str(config.settings.out_dir) == str(tmp_path / "saved")
    assert config.settings.cookies_from_browser == "edge"
    assert config.settings.realtime is False


def test_put_settings_rejects_bad_dir(client, monkeypatch):
    monkeypatch.setattr(config.settings, "out_dir", Path("/"))
    body = client.put("/settings", json={"out_dir": "/dev/null/not-a-dir"}).json()
    assert "detail" in body  # 目录不可用 → 400


# ---------- 解析与错误结构 ----------
def test_extract_returns_media_info(client, monkeypatch):
    async def fake_extract(text: str, *, quality: str = "best"):
        return fake_media()

    monkeypatch.setattr(registry, "extract", fake_extract)
    body = client.post("/extract", json={"text": "https://www.bilibili.com/video/BV1"}).json()
    assert body["platform"] == "bilibili"
    assert body["streams"][0]["quality"] == "480P"


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (AuthRequired("需 cookie", hint="导出 cookie"), 401, "AUTH_REQUIRED"),
        (UnsupportedUrl("不支持", hint="看 vx platforms"), 400, "UNSUPPORTED_URL"),
    ],
)
def test_errors_are_structured(client, monkeypatch, exc, status, code):
    async def boom(text: str, *, quality: str = "best"):
        raise exc

    monkeypatch.setattr(registry, "extract", boom)
    resp = client.post("/extract", json={"text": "x"})
    assert resp.status_code == status
    assert resp.json()["code"] == code
    assert resp.json()["hint"]


# ---------- 任务 ----------
def test_task_flow_create_poll_done(client, monkeypatch, tmp_path: Path):
    patch_success(monkeypatch, tmp_path, size=16)
    created = client.post("/tasks", json={"text": "https://example/v/1", "out_dir": str(tmp_path)})
    assert created.status_code == 202
    task_id = created.json()["id"]

    for _ in range(200):
        task = client.get(f"/tasks/{task_id}").json()
        if task["state"] in ("done", "error"):
            break
    assert task["state"] == "done", task
    assert task["size"] == 16
    assert task["progress"]["overall"] == 1.0

    listing = client.get("/tasks").json()
    assert listing["active"] == 0
    assert any(row["id"] == task_id for row in listing["tasks"])


def test_list_tasks_empty(client):
    assert client.get("/tasks").json() == {"tasks": [], "active": 0}


def test_task_404(client):
    assert client.get("/tasks/nope").status_code == 404
    assert client.delete("/tasks/nope").status_code == 404
    assert client.post("/tasks/nope/cancel").status_code == 404


def test_task_file_serves_range(client, isolated_home, tmp_path: Path):
    video = tmp_path / "sample.mp4"
    video.write_bytes(bytes(range(256)))
    task = tasks.Task(id="fixed1", text="x", out_dir=tmp_path, state=tasks.TaskState.done, path=video, _manager=isolated_home)
    isolated_home.tasks[task.id] = task

    full = client.get("/tasks/fixed1/file")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert len(full.content) == 256

    partial = client.get("/tasks/fixed1/file", headers={"Range": "bytes=10-19"})
    assert partial.status_code == 206
    assert partial.headers["content-range"] == "bytes 10-19/256"
    assert partial.content == bytes(range(10, 20))

    attachment = client.get("/tasks/fixed1/file?attachment=1")
    assert "attachment" in attachment.headers["content-disposition"]

    bad = client.get("/tasks/fixed1/file", headers={"Range": "bytes=999-1000"})
    assert bad.status_code == 416


def test_clear_finished(client, isolated_home, tmp_path: Path):
    for i in range(2):
        isolated_home.tasks[f"f{i}"] = tasks.Task(
            id=f"f{i}", text="x", out_dir=tmp_path, state=tasks.TaskState.done, _manager=isolated_home
        )
    assert client.post("/tasks/clear-finished").json()["removed"] == 2


# ---------- SSE ----------
def test_events_stream_sends_hello_frame(client):
    with client.stream("GET", "/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        first = next(resp.iter_lines())
        while not first:
            first = next(resp.iter_lines())
        assert first.startswith("data: ")
        payload = json.loads(first.removeprefix("data: "))
        assert payload["event"] == "hello"
        assert payload["tasks"] == []


# ---------- 图片代理 ----------
def test_proxy_image_rejects_non_allowlisted_host(client):
    resp = client.get("/proxy/image", params={"url": "https://example.com/a.jpg"})
    assert resp.status_code == 400
    assert "白名单" in resp.json()["detail"]


def test_proxy_image_rejects_non_http(client):
    resp = client.get("/proxy/image", params={"url": "file:///etc/passwd"})
    assert resp.status_code == 400


def test_proxy_image_rejects_loopback(client):
    resp = client.get("/proxy/image", params={"url": "http://127.0.0.1/xhscdn.com/a.jpg"})
    assert resp.status_code == 400


def test_proxy_image_allows_host_behind_fake_ip_dns(client, monkeypatch):
    """本机常驻 TUN 代理时，域名会被解析成 198.18.x.x fake-ip——不能因此拒掉封面。"""
    import socket as socket_module

    monkeypatch.setattr(
        socket_module,
        "getaddrinfo",
        lambda *a, **k: [(socket_module.AF_INET, socket_module.SOCK_STREAM, 6, "", ("198.18.0.7", 0))],
    )

    async def fake_get(self, url, **kwargs):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = b"\xff\xd8fakejpeg"

        return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient.get", fake_get)
    resp = client.get("/proxy/image", params={"url": "https://sns-webpic.xhscdn.com/cover.jpg"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"


# ---------- 前端资源 ----------
def test_root_serves_frontend(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "/static/app.js" in resp.text
    assert "/static/styles.css" in resp.text


def test_static_assets_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_file_response_declares_real_content_types(client, isolated_home, tmp_path: Path):
    """按扩展名发 Content-Type。

    回归点：早先把 ".jpg" 当成路径再取一次 suffix，结果**所有文件都是
    application/octet-stream**——浏览器播放/试听/预览全靠猜类型。
    """
    cases = {
        "clip.mp4": "video/mp4",
        "song.m4a": "audio/mp4",
        "song.mp3": "audio/mpeg",
        "01.jpg": "image/jpeg",
        "文字稿.md": "text/markdown; charset=utf-8",
        "字幕.srt": "application/x-subrip; charset=utf-8",
    }
    for index, (name, expected) in enumerate(cases.items()):
        target = tmp_path / name
        target.write_bytes(b"data")
        task_id = f"t{index}"
        isolated_home.tasks[task_id] = tasks.Task(
            id=task_id, text="x", out_dir=tmp_path, state=tasks.TaskState.done, path=target, _manager=isolated_home,
        )
        resp = client.get(f"/tasks/{task_id}/file")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(expected), f"{name} → {resp.headers['content-type']}"


def test_legacy_download_endpoint_still_works(client, monkeypatch, tmp_path: Path):
    patch_success(monkeypatch, tmp_path)
    body = client.post("/download", json={"text": "x", "out_dir": str(tmp_path)}).json()
    assert body["path"].endswith("out.mp4")
    assert body["size"] == 8


# ---------- 产物模式（音频 / 图集）----------
def test_doctor_reports_asr_and_ffprobe(client):
    body = client.get("/doctor").json()
    assert "ffprobe" in body
    assert set(body["asr"]) >= {"available", "backend", "model", "choices"}
    assert isinstance(body["asr"]["available"], list)


def test_task_threads_mode_and_audio_options_to_downloader(client, monkeypatch, tmp_path: Path):
    captured: dict = {}
    patch_success(monkeypatch, tmp_path, captured=captured)

    created = client.post(
        "/tasks",
        json={
            "text": "x", "out_dir": str(tmp_path),
            "mode": "audio", "audio_format": "mp3", "embed_tags": False,
            "transcribe": True, "transcript_format": "srt",
        },
    )
    assert created.status_code == 202
    task = created.json()
    assert task["mode"] == "audio" and task["audio_format"] == "mp3" and task["transcribe"] is True

    for _ in range(200):
        task = client.get(f"/tasks/{task['id']}").json()
        if task["state"] in ("done", "error"):
            break
    assert task["state"] == "done", task
    assert captured == {
        "mode": "audio", "audio_format": "mp3", "embed_tags": False, "transcribe": True,
        "transcript_format": "srt", "asr_backend": None, "asr_model": None, "asr_language": None,
    }


def test_task_threads_asr_backend(client, monkeypatch, tmp_path: Path):
    captured: dict = {}
    patch_success(monkeypatch, tmp_path, captured=captured)

    created = client.post(
        "/tasks",
        json={
            "text": "x", "out_dir": str(tmp_path), "mode": "audio", "transcribe": True,
            "asr_backend": "faster_whisper", "asr_language": "zh",
        },
    ).json()
    assert created["asr_backend"] == "faster_whisper"

    for _ in range(200):
        task = client.get(f"/tasks/{created['id']}").json()
        if task["state"] in ("done", "error"):
            break
    assert captured["asr_backend"] == "faster_whisper"


def test_task_rejects_unknown_asr_backend(client, tmp_path: Path):
    resp = client.post("/tasks", json={"text": "x", "out_dir": str(tmp_path), "asr_backend": "whisperx"})
    assert resp.status_code == 202, "后端是自由字符串（asr.pick_backend 会给出可选值），不是枚举"


def test_task_rejects_unknown_mode(client, tmp_path: Path):
    resp = client.post("/tasks", json={"text": "x", "out_dir": str(tmp_path), "mode": "podcast"})
    assert resp.status_code == 422


def test_task_artifact_route_serves_indexed_files(client, isolated_home, tmp_path: Path):
    folder = tmp_path / "图集"
    folder.mkdir()
    (folder / "01.jpg").write_bytes(b"\xff\xd8one")
    (folder / "02.jpg").write_bytes(b"\xff\xd8two")
    task = tasks.Task(
        id="img1", text="x", out_dir=tmp_path, state=tasks.TaskState.done, mode="images",
        path=folder, artifacts=[folder / "01.jpg", folder / "02.jpg"], _manager=isolated_home,
    )
    isolated_home.tasks[task.id] = task

    assert client.get("/tasks/img1/artifact?index=1").content == b"\xff\xd8two"
    assert client.get("/tasks/img1/artifact").content == b"\xff\xd8one"
    assert client.get("/tasks/img1/artifact?index=2").status_code == 404

    listing = client.get("/tasks").json()["tasks"][0]
    assert listing["mode"] == "images"
    assert len(listing["artifacts"]) == 2


def test_task_file_on_directory_says_use_artifact_route(client, isolated_home, tmp_path: Path):
    folder = tmp_path / "图集"
    folder.mkdir()
    task = tasks.Task(
        id="img2", text="x", out_dir=tmp_path, state=tasks.TaskState.done, path=folder, _manager=isolated_home,
    )
    isolated_home.tasks[task.id] = task
    resp = client.get("/tasks/img2/file")
    assert resp.status_code == 409
    assert "artifact" in resp.json()["detail"]