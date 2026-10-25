"""HTTP API + 内置前端：`vx serve` 之后所有操作都能在浏览器里完成。

四类接口：
1. 信息：/health /doctor /platforms /settings
2. 单次解析：/extract（给前端预览用，不落盘）
3. 任务队列：/tasks + /events(SSE)（下载、进度、取消、播放、打开目录）—— 前端主流程
4. 资源：/tasks/{id}/file（带 Range，可内嵌播放）、/proxy/image（封面防盗链代理）
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shutil
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse as parse_url

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import asr, capabilities, config, cookies, download, engine, registry
from ..errors import VextractError
from ..models import AudioFormat, Mode, TranscriptFormat
from ..tasks import TERMINAL, manager

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 封面/图片代理白名单：只允许这些平台 CDN，避免把本地服务变成开放代理
IMAGE_HOST_SUFFIXES = (
    "xhscdn.com", "sinaimg.cn", "hdslb.com", "douyinpic.com", "douyin.com",
    "kwimgs.com", "yximgs.com", "ibyteimg.com", "byteimg.com", "pstatp.com",
    "weibocdn.com", "tiktokcdn.com", "ytimg.com", "ggpht.com", "googleusercontent.com",
    "bilivideo.com", "bilivideo.cn", "kwai.com", "kuaishou.com",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.load_persisted()
    manager.load_history()
    yield


app = FastAPI(title="vextract", version="0.3.0", description="自媒体视频提取 API + 前端", lifespan=lifespan)


class ExtractRequest(BaseModel):
    text: str = Field(description="分享文本或链接")
    quality: str = "best"


class TaskRequest(BaseModel):
    text: str = Field(description="分享文本或链接")
    quality: str = "best"
    stream_index: int | None = None
    filename: str | None = None
    out_dir: str | None = None
    mode: Mode | None = Field(default=None, description="video / audio / images；留空按内容推断")
    audio_format: AudioFormat = "m4a"
    embed_tags: bool = True
    transcribe: bool = Field(default=False, description="音频落盘后再跑本地 ASR 出文字稿")
    transcript_format: TranscriptFormat = "md"
    asr_model: str | None = None
    asr_language: str | None = None


class SettingsPatchModel(config.SettingsPatch):
    pass


@app.exception_handler(VextractError)
async def _vextract_error(_request: Request, exc: VextractError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


# ---------- 信息 ----------
@app.get("/health")
async def health() -> dict:
    try:
        version = engine.engine_version()
    except Exception as exc:  # noqa: BLE001
        version = f"unavailable: {exc}"
    return {
        "status": "ok",
        "version": app.version,
        "yt_dlp": version,
        "ffmpeg": download.ffmpeg_path(),
        "out_dir": str(config.settings.out_dir),
    }


@app.get("/doctor")
async def doctor() -> dict:
    """与 `vx doctor` 同一份数据，前端「平台」页直接展示。"""
    cookies_rows = [
        {
            "platform": platform,
            "cookie": cookies.describe(platform),
            "path": str(config.settings.cookie_dir / f"{platform}.txt"),
        }
        for platform in ("douyin", "bilibili", "xhs", "kuaishou", "weibo")
    ]
    try:
        version = engine.engine_version()
    except Exception as exc:  # noqa: BLE001
        version = f"unavailable: {exc}"
    return {
        "ok": bool(download.ffmpeg_path()),
        "yt_dlp": version,
        "ffmpeg": download.ffmpeg_path(),
        "ffprobe": shutil.which("ffprobe"),
        "asr": asr.describe(),
        "python": sys.version.split()[0],
        "out_dir": str(config.settings.out_dir),
        "cookie_dir": str(config.settings.cookie_dir),
        "cookies": cookies_rows,
        "providers": [p.name for p in registry.all_providers()],
    }


@app.get("/platforms")
async def platforms() -> dict:
    return {"platforms": capabilities.CAPABILITIES, "providers": [p.name for p in registry.all_providers()]}


@app.get("/settings")
async def get_settings() -> dict:
    return config.as_dict()


@app.put("/settings")
async def put_settings(patch: SettingsPatchModel) -> dict:
    if patch.out_dir:
        target = Path(patch.out_dir).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"输出目录不可用：{exc}") from exc
    return config.save(patch)


# ---------- 解析 ----------
@app.post("/extract")
async def extract(payload: ExtractRequest) -> dict:
    media = await registry.extract(payload.text, quality=payload.quality)
    return media.model_dump()


# ---------- 任务 ----------
@app.get("/tasks")
async def list_tasks() -> dict:
    return {"tasks": manager.snapshot(), "active": sum(1 for t in manager.tasks.values() if t.state not in TERMINAL)}


@app.post("/tasks", status_code=202)
async def create_task(payload: TaskRequest) -> dict:
    out_dir = Path(payload.out_dir).expanduser() if payload.out_dir else config.settings.out_dir
    task = manager.create(
        text=payload.text,
        out_dir=out_dir,
        quality=payload.quality,
        stream_index=payload.stream_index,
        filename=payload.filename,
        mode=payload.mode,
        audio_format=payload.audio_format,
        embed_tags=payload.embed_tags,
        transcribe=payload.transcribe,
        transcript_format=payload.transcript_format,
        asr_model=payload.asr_model,
        asr_language=payload.asr_language,
    )
    return task.serialize()


@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.serialize()


@app.post("/tasks/clear-finished")
async def clear_finished() -> dict:
    return {"removed": manager.clear_finished()}


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    task = manager.cancel(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.serialize()


@app.delete("/tasks/{task_id}")
async def delete_task(task_id: str) -> dict:
    if not manager.remove(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@app.post("/tasks/{task_id}/reveal")
async def reveal_task(task_id: str) -> dict:
    """在访达里定位已下载文件（只有用户点击才触发）。"""
    task = manager.get(task_id)
    if not task or not task.path or not task.path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if sys.platform != "darwin":
        return {"ok": False, "message": "仅 macOS 支持在访达中显示"}
    subprocess.Popen(["open", "-R", str(task.path)])  # noqa: S603,S607
    return {"ok": True, "path": str(task.path)}


@app.get("/tasks/{task_id}/file")
async def task_file(task_id: str, request: Request, attachment: int = 0):
    """内嵌播放（支持 Range，能拖进度）或作为附件下载。"""
    task = manager.get(task_id)
    if not task or not task.path or not task.path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if task.path.is_dir():
        raise HTTPException(
            status_code=409,
            detail=(
                f"这个任务的产物是目录（{task.path.name}）："
                f"用 /tasks/{task_id}/artifact?index=N 取单张，或「在访达中显示」"
            ),
        )
    return _file_response(task.path, request, as_attachment=bool(attachment))


@app.get("/tasks/{task_id}/artifact")
async def task_artifact(task_id: str, request: Request, index: int = 0, attachment: int = 0):
    """主产物之外的文件：图集的第 N 张图（index 从 0 起）、转录出的文字稿。

    图集任务的 path 是目录，不能走 /file——这条路由就是给它们用的。
    """
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not 0 <= index < len(task.artifacts):
        raise HTTPException(status_code=404, detail=f"没有第 {index} 个附加文件（共 {len(task.artifacts)} 个）")
    target = task.artifacts[index]
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件已不在：{target}")
    return _file_response(target, request, as_attachment=bool(attachment))


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    """SSE：任务状态与进度统一从这里推。"""
    queue = manager.subscribe()

    async def stream():
        try:
            yield f"data: {json.dumps({'event': 'hello', 'tasks': manager.snapshot()}, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- 图片代理（封面防盗链） ----------
@app.get("/proxy/image")
async def proxy_image(url: str) -> StreamingResponse:
    parsed = parse_url(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="只允许 http/https")
    host = (parsed.hostname or "").lower()
    if not any(host == suffix or host.endswith("." + suffix) for suffix in IMAGE_HOST_SUFFIXES):
        raise HTTPException(status_code=400, detail=f"不在白名单内的图片域名：{host}")
    _reject_private_host(host)

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cli:
        try:
            resp = await cli.get(url, headers={"Referer": f"https://{host}/", "User-Agent": "Mozilla/5.0"})
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"取图失败：{exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"取图失败：HTTP {resp.status_code}")
    return StreamingResponse(
        iter([resp.content]),
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ---------- 兼容旧接口（CLI/脚本用） ----------
@app.post("/download")
async def download_endpoint(payload: TaskRequest) -> dict:
    media = await registry.extract(payload.text, quality=payload.quality)
    path = await download.download(
        media,
        out_dir=Path(payload.out_dir).expanduser() if payload.out_dir else config.settings.out_dir,
        quality=payload.quality,
        stream_index=payload.stream_index,
        filename=payload.filename,
        mode=payload.mode,
        audio_format=payload.audio_format,
        embed_tags=payload.embed_tags,
        transcribe=payload.transcribe,
        transcript_format=payload.transcript_format,
        asr_model=payload.asr_model,
        asr_language=payload.asr_language,
    )
    return {"path": str(path), "size": path.stat().st_size, "media": media.model_dump()}


# ---------- 前端 ----------
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>前端文件缺失</h1><p>请重新安装：uv sync</p>", status_code=500)
    return HTMLResponse(page.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ---------- 工具 ----------
def _reject_private_host(host: str) -> None:
    """挡掉明显不该出现的目标（回环/链路本地/组播/IP 字面量的内网）。

    注意：**不能**按"私有网段"一刀切——本机常驻 TUN 代理（FlClash/Clash）会把外部域名
    解析成 198.18.x.x 这类 fake-ip，一刀切会导致所有封面都被拒。真正的防线是上面的域名白名单。
    """
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"域名解析失败：{host}") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    else:
        addresses = [literal]

    for address in addresses:
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            raise HTTPException(status_code=400, detail="拒绝访问本机/链路本地地址")


_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".webm": "video/webm",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".srt": "application/x-subrip; charset=utf-8",
}


def _media_type(path: Path) -> str:
    """按扩展名给 Content-Type。

    这里踩过一次：早先写的是 `PurePosixPath(path.suffix.lower()).suffix`——把 ".jpg"
    当成路径再取一次后缀，结果永远拿到空串，**所有文件都发成 application/octet-stream**
    （视频内嵌播放、音频试听、图片预览全靠浏览器猜类型）。直接用一次 suffix 就好。
    """
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _file_response(path: Path, request: Request, *, as_attachment: bool = False):
    """带 Range 支持的文件响应，让浏览器里能拖动进度条。"""
    size = path.stat().st_size
    disposition = "attachment" if as_attachment else "inline"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(path.name)}",
    }
    range_header = request.headers.get("range")
    if not range_header or not range_header.startswith("bytes="):
        return FileResponse(path, media_type=_media_type(path), headers=headers)

    spec = range_header.removeprefix("bytes=").split(",")[0].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        start = int(start_text) if start_text else 0
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return FileResponse(path, media_type=_media_type(path), headers=headers)
    start = max(0, start)
    end = min(end, size - 1)
    if start > end:
        raise HTTPException(status_code=416, detail="请求范围非法")

    def body():
        remaining = end - start + 1
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                data = handle.read(min(512 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
    return StreamingResponse(body(), status_code=206, media_type=_media_type(path), headers=headers)