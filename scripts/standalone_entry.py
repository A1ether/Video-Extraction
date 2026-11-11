"""PyInstaller 冻结后的入口：双击 .app 时启动服务并打开浏览器。

这个文件只用于独立包构建（scripts/build_standalone_app.py），正常运行源码时用 `vx serve`。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import traceback
import urllib.request
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("VEXTRACT_PORT", "8787"))
SCAN_PORTS = 13


def bundle_root() -> Path:
    """PyInstaller 的 _MEIPASS（one-folder 模式即 Contents/Frameworks）。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def setup_log() -> None:
    log_dir = Path.home() / "Library" / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "vextract.log").open("a", buffering=1, encoding="utf-8")
    sys.stdout = log
    sys.stderr = log
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)


def setup_path() -> None:
    """把随包的 ffmpeg/ffprobe 放到 PATH 最前面；没有则保持系统 PATH。"""
    for candidate in (
        bundle_root() / "ffmpeg" / "bin",
        bundle_root().parent / "Resources" / "ffmpeg" / "bin",
    ):
        if (candidate / "ffmpeg").exists():
            os.environ["PATH"] = f"{candidate}{os.pathsep}{os.environ.get('PATH', '')}"
            return


def instance_url() -> str | None:
    for offset in range(SCAN_PORTS):
        url = f"http://127.0.0.1:{PORT + offset}/"
        try:
            with urllib.request.urlopen(f"{url}health", timeout=0.6) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("status") == "ok" and "yt_dlp" in payload:
                return url
        except Exception:  # noqa: BLE001 — 端口没开/不是本程序，继续找
            continue
    return None


def alert(message: str) -> None:
    with contextlib.suppress(Exception):  # 连弹窗都失败时只剩日志
        subprocess.run(
            ["osascript", "-e", f'display alert "vextract" message {json.dumps(message)} as critical'],
            check=False,
            capture_output=True,
            timeout=10,
        )


def main() -> None:
    setup_log()
    setup_path()
    try:
        no_browser = os.environ.get("VEXTRACT_NO_BROWSER") == "1"
        existing = instance_url()
        if existing:
            if not no_browser:
                webbrowser.open(existing)
            return
        from vextract.cli import app as cli

        args = ["serve", "--port", str(PORT)]
        if no_browser:
            args.append("--no-open")
        cli(args=args)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — 冻结环境里没有终端，必须落日志 + 弹窗
        traceback.print_exc()
        alert(f"启动失败：{type(exc).__name__}: {exc}\n日志：~/Library/Logs/vextract.log")
        raise
    finally:
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.flush()


if __name__ == "__main__":
    main()
