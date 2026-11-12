"""PyInstaller 冻结后的入口：双击 .app 时确保后台服务在跑，并打开浏览器。

设计要点：**启动器进程只负责"确保服务 + 开浏览器"，然后退出**。
服务本身用 `start_new_session` 脱离启动器常驻；这样下次双击时 LaunchServices
不会把点击解释成"激活一个没有窗口的旧进程"，而是真的再跑一次启动器。
（此前服务就是 app 本体：浏览器关掉后再双击，系统只是 activate 旧进程，
既没有窗口也不会开浏览器，看起来就是"打不开"。）

这个文件只用于独立包构建（scripts/build_standalone_app.py），正常运行源码时用 `vx serve`。
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path
from typing import TextIO

PORT = int(os.environ.get("VEXTRACT_PORT", "8787"))
SCAN_PORTS = 13
INTERNAL_SERVER = "VEXTRACT_INTERNAL_SERVER"


def bundle_root() -> Path:
    """PyInstaller 的 _MEIPASS（one-folder 模式即 Contents/Frameworks）。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def setup_log() -> TextIO:
    log_dir = Path.home() / "Library" / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "vextract.log").open("a", buffering=1, encoding="utf-8")
    sys.stdout = log
    sys.stderr = log
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    return log


def setup_path() -> None:
    """把随包的 ffmpeg/ffprobe 放到 PATH 最前面，并保证系统目录一定在。"""
    standard = "/usr/bin:/bin:/usr/sbin:/sbin"
    existing = os.environ.get("PATH", "")
    for candidate in (
        bundle_root() / "ffmpeg" / "bin",
        bundle_root().parent / "Resources" / "ffmpeg" / "bin",
    ):
        if (candidate / "ffmpeg").exists():
            os.environ["PATH"] = f"{candidate}{os.pathsep}{standard}{os.pathsep}{existing}"
            return
    os.environ["PATH"] = f"{standard}{os.pathsep}{existing}"


def instance_url() -> str | None:
    for offset in range(SCAN_PORTS):
        url = f"http://127.0.0.1:{PORT + offset}/"
        try:
            with urllib.request.urlopen(f"{url}health", timeout=0.5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("status") == "ok" and "yt_dlp" in payload:
                return url
        except Exception:  # noqa: BLE001 — 端口没开/不是本程序，继续找
            continue
    return None


def start_detached_server(log: TextIO) -> str | None:
    """启动一个脱离启动器的服务进程，等它健康后返回 URL。"""
    cmd = [sys.executable]
    if not getattr(sys, "frozen", False):
        cmd.append(str(Path(__file__).resolve()))

    env = os.environ.copy()
    env[INTERNAL_SERVER] = "1"
    env["VEXTRACT_NO_BROWSER"] = "1"  # 浏览器由启动器开，服务自己不要开
    subprocess.Popen(
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        close_fds=True,
    )

    for _ in range(40):  # 最多等 20 秒
        time.sleep(0.5)
        url = instance_url()
        if url:
            return url
    return None


def alert(message: str) -> None:
    with contextlib.suppress(Exception):  # 连弹窗都失败时只剩日志
        subprocess.run(
            ["osascript", "-e", f'display alert "vextract" message {json.dumps(message)} as critical'],
            check=False,
            capture_output=True,
            timeout=10,
        )


def serve() -> None:
    """服务进程本体（被启动器以 INTERNAL_SERVER=1 拉起）。"""
    from vextract.cli import app as cli

    args = ["serve", "--port", str(PORT)]
    if os.environ.get("VEXTRACT_NO_BROWSER") == "1":
        args.append("--no-open")
    cli(args=args)


def watch_parent() -> None:
    """原生外壳被强退时，子进程会被 launchd 收养；发现 PPID 变化就退出。"""
    parent = os.getppid()

    def run() -> None:
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=run, daemon=True).start()


def main() -> None:
    log = setup_log()
    setup_path()

    # 原生 Swift/WKWebView 外壳：服务直接在前台跑，父进程负责窗口和退出。
    if os.environ.get("VEXTRACT_NATIVE_SERVER") == "1":
        watch_parent()
        serve()
        return

    is_server = os.environ.get(INTERNAL_SERVER) == "1"
    try:
        if is_server:
            serve()
            return

        no_browser = os.environ.get("VEXTRACT_NO_BROWSER") == "1"
        existing = instance_url()
        if existing:
            # 后台服务已在跑：这次双击只负责把界面叫出来，然后退出。
            print(f"打开界面：{existing}")
            if not no_browser:
                webbrowser.open(existing)
            return

        url = start_detached_server(log)
        if url is None:
            alert("后台服务没有在 20 秒内启动。\n日志：~/Library/Logs/vextract.log")
            return
        print(f"打开界面：{url}")
        if not no_browser:
            webbrowser.open(url)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — 冻结环境里没有终端，必须落日志 + 弹窗
        traceback.print_exc()
        if not is_server:
            alert(f"启动失败：{type(exc).__name__}: {exc}\n日志：~/Library/Logs/vextract.log")
        raise
    finally:
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.flush()


if __name__ == "__main__":
    main()
