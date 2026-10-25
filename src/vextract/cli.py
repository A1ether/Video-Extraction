"""vx 命令行：platforms / doctor / info / get / serve。

错误呈现规则：CLI 只打印 code + hint，不把平台原始堆栈丢给用户（ADR-004）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import cast

import typer
from rich.console import Console
from rich.json import JSON
from rich.table import Table

from . import asr, capabilities, config, cookies, download, engine, ports, registry
from .errors import CookieExportInvalid, VextractError
from .models import AudioFormat, Mode, TranscriptFormat

app = typer.Typer(add_completion=False, help="自媒体视频提取工具：分享链接 → 无水印视频 + 元数据")
console = Console()

cookies_app = typer.Typer(help="cookie 阶梯：导入浏览器导出、查看当前状态")
app.add_typer(cookies_app, name="cookies")

BANNER = "vextract"


def _path_size(path: Path) -> int:
    """产物可能是文件（视频/音频）也可能是目录（图集），体积都要算对。"""
    if path.is_dir():
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return path.stat().st_size


def _choice(value: str | None, allowed: tuple[str, ...], what: str) -> str | None:
    """把非法取值变成带 hint 的错误，而不是让 typer 抛一段看不出所以然的堆栈。"""
    if value is None or value in allowed:
        return value
    _fail(
        VextractError(
            f"{what} 只能是 {'/'.join(allowed)}，收到 {value!r}",
            hint=f"例：vx get '<链接>' --{what} {allowed[0]}",
        )
    )
    return None


def _fail(exc: VextractError) -> None:
    console.print(f"[bold red]{exc.code}[/bold red] {exc.message}")
    if exc.hint:
        for line in exc.hint.splitlines():
            console.print(f"  [yellow]→[/yellow] {line}")
    if exc.detail:
        console.print(f"  [dim]{exc.detail}[/dim]")
    raise typer.Exit(code=1)


@app.command("platforms")
def platforms() -> None:
    """平台能力矩阵（含本机实测状态）。"""
    table = Table(title=f"{BANNER} · 平台能力", show_lines=True)
    for column in ("平台", "解析方式", "鉴权", "合并", "实测"):
        table.add_column(column, overflow="fold")
    for row in capabilities.CAPABILITIES:
        table.add_row(row["platform"], row["resolver"], row["auth"], row["merge"], row["verified"])
    console.print(table)
    console.print(f"[dim]provider 加载顺序：{[p.name for p in registry.all_providers()]}[/dim]")


@app.command("doctor")
def doctor() -> None:
    """自检：引擎、ffmpeg、cookie、网络出口。"""
    table = Table(title=f"{BANNER} · 环境自检")
    table.add_column("项")
    table.add_column("状态", overflow="fold")

    ff = download.ffmpeg_path()
    table.add_row("ffmpeg", f"[green]{ff}[/green]" if ff else "[red]未找到（B站/YouTube 无法合并）[/red]")
    ffprobe = shutil.which("ffprobe")
    table.add_row(
        "ffprobe",
        f"[green]{ffprobe}[/green]" if ffprobe else "[yellow]未找到（音频模式会退回「总是转码」，仍能出文件）[/yellow]",
    )
    status = asr.describe()
    if status["available"]:
        table.add_row("本地转录", f"[green]{status['available'][0]}[/green] · 默认模型 {status['model']}")
    else:
        table.add_row("本地转录", "[yellow]未安装（uv sync --extra transcribe）[/yellow]")
    try:
        table.add_row("yt-dlp", f"[green]{engine.engine_version()}[/green]")
    except Exception as exc:  # noqa: BLE001
        table.add_row("yt-dlp", f"[red]不可用：{exc}[/red]")
    table.add_row("python", sys.version.split()[0])
    table.add_row("cookie 目录", str(config.settings.cookie_dir))
    for platform in ("douyin", "bilibili", "xhs", "kuaishou", "weibo"):
        table.add_row(f"cookie: {platform}", cookies.describe(platform))
    table.add_row("默认输出目录", str(config.settings.out_dir))
    console.print(table)


@cookies_app.command("import")
def cookies_import(
    source: Path = typer.Argument(..., help="Cookie-Editor 的 JSON 导出，或 Netscape 格式的 cookies.txt"),
    platform: str = typer.Option("douyin", "--platform", "-p", help="douyin/bilibili/xhs/kuaishou/weibo"),
) -> None:
    """把浏览器导出的 cookie 落成 <platform>.txt（Netscape、权限 600）。"""
    if not source.exists():
        _fail(CookieExportInvalid(f"找不到文件 {source}", hint="检查路径；导出后直接拖进来即可"))
        return
    try:
        text = source.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        _fail(CookieExportInvalid(f"读不了 {source}：{exc}", hint="确认文件可读（插件导出的是文本，不是二进制）"))
        return
    try:
        result = cookies.import_export(text, platform)
    except ValueError as exc:
        _fail(
            CookieExportInvalid(
                str(exc),
                hint="在 Cookie-Editor 里选 Export → JSON，或在扩展页右键导出 cookies.txt",
                platform=platform,
            )
        )
        return
    console.print(
        f"[bold green]✔ 已写入[/bold green] {result.path} "
        f"[dim]({result.written} 条 cookie，权限 600)[/dim]"
    )
    for note in result.skipped:
        console.print(f"  [yellow]跳过[/yellow] {note}")
    console.print(f"[dim]验证：vx cookies show {platform}；用它真跑一条：vx get '<分享链接>'[/dim]")


@cookies_app.command("show")
def cookies_show(
    platform: str = typer.Argument(..., help="douyin/bilibili/xhs/kuaishou/weibo"),
) -> None:
    """看某个平台当前用的是哪份 cookie（只列名字，不打印值）。"""
    console.print(cookies.summarize(platform))


@app.command("info")
def info(
    text: str = typer.Argument(..., help="分享文本或链接"),
    as_json: bool = typer.Option(False, "--json", help="输出原始 JSON"),
    cookies_from_browser: str | None = typer.Option(None, "--cookies-from-browser", help="chrome/firefox/edge 等"),
) -> None:
    """只解析不下载：看看能拿到什么。"""
    config.set_cookies_from_browser(cookies_from_browser)
    try:
        media = asyncio.run(registry.extract(text))
    except VextractError as exc:
        _fail(exc)
        return
    if as_json:
        console.print(JSON(json.dumps(media.model_dump(), ensure_ascii=False)))
        return
    console.print(f"[bold]{media.summary()}[/bold] · {media.work_kind}")
    if media.assets:
        asset_table = Table(show_header=True, header_style="bold", title="图集")
        for column in ("#", "尺寸", "格式"):
            asset_table.add_column(column)
        for asset in sorted(media.assets, key=lambda a: a.index):
            side = f"{asset.width}x{asset.height}" if asset.width and asset.height else "—"
            asset_table.add_row(str(asset.index), side, asset.ext)
        console.print(asset_table)
        console.print("[dim]图文作品：下载用 `vx get <链接> --mode images`（或前端「图集」模式）[/dim]")
    if media.quality_note:
        console.print(f"[yellow]提示：{media.quality_note}[/yellow]")
    table = Table(show_header=True, header_style="bold")
    for column in ("#", "质量", "类型", "编码", "码率", "大小"):
        table.add_column(column)
    for i, stream in enumerate(sorted(media.streams, key=lambda s: (s.audio_only, -s.rank)), 1):
        size = f"{stream.filesize / 1048576:.1f}MB" if stream.filesize else "-"
        bitrate = f"{int((stream.bitrate or 0) / 1000)}k" if stream.bitrate else "-"
        table.add_row(
            str(i),
            stream.quality,
            "音频" if stream.audio_only else "视频",
            stream.codec or "-",
            bitrate,
            size,
        )
    console.print(table)
    if media.needs_merge:
        console.print("[dim]该平台音视频分离，下载时会调用 ffmpeg 无损合并（-c copy）[/dim]")


@app.command("get")
def get(
    text: str = typer.Argument(..., help="分享文本或链接"),
    out: Path = typer.Option(None, "-o", "--out", help="输出目录，默认 ~/Downloads"),
    quality: str = typer.Option("best", "-q", "--quality", help="best/1080p/720p/480p；`audio` 等同 --mode audio"),
    filename: str = typer.Option(None, "-f", "--filename", help="自定义文件名（不含扩展名）"),
    cookies_from_browser: str | None = typer.Option(None, "--cookies-from-browser", help="chrome/firefox/edge 等"),
    mode: str | None = typer.Option(None, "--mode", help="video/audio/images；留空按内容推断（图文→images）"),
    audio_format: str = typer.Option("m4a", "--audio-format", help="仅音频模式的容器：m4a/mp3/flac"),
    no_tags: bool = typer.Option(False, "--no-tags", help="不把标题/作者/来源写进音频标签"),
    transcribe: bool = typer.Option(False, "--transcribe", help="音频落盘后再跑本地 ASR，输出文字稿"),
    transcript_format: str = typer.Option("md", "--transcript-format", help="文字稿格式：md/txt/srt"),
    asr_model: str | None = typer.Option(None, "--asr-model", help="ASR 模型（默认按后端取值，见 vx doctor）"),
    asr_language: str | None = typer.Option(None, "--asr-language", help="zh/en/…；留空让模型自己判断"),
) -> None:
    """解析并下载：视频 / 仅音频 / 图集，可选本地转录。"""
    config.set_cookies_from_browser(cookies_from_browser)
    mode = cast("Mode | None", _choice(mode, ("video", "audio", "images"), "mode"))
    audio_format = cast("AudioFormat", _choice(audio_format, ("m4a", "mp3", "flac"), "audio-format") or "m4a")
    transcript_format = cast(
        "TranscriptFormat", _choice(transcript_format, ("md", "txt", "srt"), "transcript-format") or "md"
    )

    try:
        media = asyncio.run(registry.extract(text, quality=quality))
    except VextractError as exc:
        _fail(exc)
        return
    console.print(f"[bold]{media.summary()}[/bold] · {media.work_kind}")
    if media.quality_note:
        console.print(f"[yellow]提示：{media.quality_note}[/yellow]")

    artifacts: list[Path] = []
    try:
        path = asyncio.run(
            download.download(
                media,
                out_dir=out or config.settings.out_dir,
                quality=quality,
                filename=filename,
                mode=mode,
                audio_format=audio_format,
                embed_tags=not no_tags,
                transcribe=transcribe,
                transcript_format=transcript_format,
                asr_model=asr_model,
                asr_language=asr_language,
                progress=_progress,
                on_artifact=artifacts.append,
            )
        )
    except VextractError as exc:
        _fail(exc)
        return
    size = _path_size(path)
    console.print(f"[bold green]✔ 完成[/bold green] {path}  ({size / 1048576:.1f} MB)")
    for artifact in artifacts:
        if artifact.suffix in (".md", ".txt", ".srt"):
            console.print(f"  [green]文字稿[/green] {artifact}")
        elif len(artifacts) <= 12:
            console.print(f"  [dim]{artifact.name}[/dim]")
    if len(artifacts) > 12:
        console.print(f"  [dim]… 共 {len(artifacts)} 个文件[/dim]")


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(ports.DEFAULT_PORT, help="端口（被占用会自动向后找空闲端口）"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="启动成功后自动打开浏览器"),
) -> None:
    """启动 HTTP 服务 + 内置前端：所有操作都在浏览器里完成。"""
    try:
        port, notice = ports.ensure_port(host, port)
    except OSError as exc:
        console.print(f"[bold red]启动失败[/bold red] {exc}")
        console.print("  换一个端口再试：vx serve --port 9000")
        raise typer.Exit(code=1) from None
    if notice:
        console.print(f"[yellow]{notice}[/yellow]")

    # 预检只是"友好提示"，真正的判据是实际绑定：uvicorn 绑定失败会 sys.exit(1)，
    # 这里接住它并换端口重试，避免"服务没起来、浏览器却打开了别人家页面"。
    for attempt in range(5):
        url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
        try:
            _serve_once(host, port, url, open_browser)
            return
        except SystemExit:
            holder = ports.occupant(port)
            next_port = ports.find_free_port(host, port + 1)
            if next_port is None or attempt == 4:
                console.print(
                    f"[bold red]端口 {port} 无法监听[/bold red]（{holder or '未知占用'}），"
                    "请手动指定：vx serve --port 9000"
                )
                raise typer.Exit(code=1) from None
            console.print(f"[yellow]端口 {port} 绑定失败（{holder or '未知占用'}），改用 {next_port}[/yellow]")
            port = next_port


def _serve_once(host: str, port: int, url: str, open_browser: bool) -> None:
    """起一次服务；绑定失败时 uvicorn 会 sys.exit(1)，由调用方换端口重试。"""
    import asyncio

    import uvicorn

    config = uvicorn.Config("vextract.api.app:app", host=host, port=port, log_level="info")
    server = uvicorn.Server(config)

    async def run() -> None:
        async def announce() -> None:
            # 关键：等到服务真的在监听、并且探活确认"这就是我们的服务"，才打开浏览器。
            for _ in range(120):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            else:
                console.print("[bold red]服务未能启动[/bold red]（端口可能刚被别的进程抢占）")
                return

            import httpx

            ready = False
            try:
                async with httpx.AsyncClient(timeout=3) as cli:
                    payload = (await cli.get(f"{url}health")).json()
                ready = isinstance(payload, dict) and "yt_dlp" in payload
            except Exception:  # noqa: BLE001 — 探活失败就不自动开浏览器
                ready = False

            if not ready:
                console.print(f"[yellow]服务已监听 {url}，但探活不通过，未自动打开浏览器[/yellow]")
                return
            console.print(f"[bold green]vextract 已就绪[/bold green] → 在浏览器打开 [cyan]{url}[/cyan]（Ctrl+C 停止）")
            if open_browser:
                import contextlib
                import webbrowser

                with contextlib.suppress(Exception):
                    webbrowser.open(url)

        task = asyncio.create_task(announce())
        console.print(f"[dim]正在启动 {host}:{port} …[/dim]")
        try:
            await server.serve()
        finally:
            task.cancel()

    asyncio.run(run())


def _progress(event: download.Progress) -> None:
    label = event.stage + (f"（{event.message}）" if event.message and event.stage != "完成" else "")
    done = event.done
    total = event.total
    if total:
        pct = done / total * 100
        sys.stderr.write(f"\r{label}: {done / 1048576:.1f}/{total / 1048576:.1f} MB ({pct:.0f}%)")
    else:
        sys.stderr.write(f"\r{label}: {done / 1048576:.1f} MB")
    sys.stderr.flush()
    if total and done >= total:
        sys.stderr.write("\n")


if __name__ == "__main__":
    app()