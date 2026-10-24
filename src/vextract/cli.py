"""vx 命令行：platforms / doctor / info / get / serve。

错误呈现规则：CLI 只打印 code + hint，不把平台原始堆栈丢给用户（ADR-004）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.json import JSON
from rich.table import Table

from . import capabilities, config, cookies, download, engine, ports, registry
from .errors import VextractError

app = typer.Typer(add_completion=False, help="自媒体视频提取工具：分享链接 → 无水印视频 + 元数据")
console = Console()

BANNER = "vextract"


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
    console.print(f"[bold]{media.summary()}[/bold]")
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
    quality: str = typer.Option("best", "-q", "--quality", help="best/1080p/720p/480p"),
    filename: str = typer.Option(None, "-f", "--filename", help="自定义文件名（不含扩展名）"),
    cookies_from_browser: str | None = typer.Option(None, "--cookies-from-browser", help="chrome/firefox/edge 等"),
) -> None:
    """解析并下载（必要时自动 ffmpeg 合并）。"""
    config.set_cookies_from_browser(cookies_from_browser)
    try:
        media = asyncio.run(registry.extract(text, quality=quality))
        console.print(f"[bold]{media.summary()}[/bold]")
        if media.quality_note:
            console.print(f"[yellow]提示：{media.quality_note}[/yellow]")
        path = asyncio.run(
            download.download(
                media,
                out_dir=out or config.settings.out_dir,
                quality=quality,
                filename=filename,
                progress=_progress,
            )
        )
    except VextractError as exc:
        _fail(exc)
        return
    size = path.stat().st_size / 1048576
    console.print(f"[bold green]✔ 完成[/bold green] {path}  ({size:.1f} MB)")


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
    label = event.stage
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