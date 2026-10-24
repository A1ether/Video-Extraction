"""端口探测：避免"浏览器打开的是别人家页面"这类事故。

背景（真实事故）：本机常驻一个 `claude-science` 守护进程占着 8000 端口，
而 `vx serve` 默认也用 8000 且**先开浏览器后启动服务** → 浏览器加载到别人的 UI，
用户被要求登录 Claude。所以这里提供：占用探测、自动让位、占用者识别。
"""

from __future__ import annotations

import socket
import subprocess

DEFAULT_PORT = 8787


def is_available(host: str, port: int) -> bool:
    """能否在该地址上监听。

    两个坑都在这里踩过：
    1) 不能无条件同时试 IPv4/IPv6（对 127.0.0.1 试 AF_INET6 必然失败 → 所有端口都被判成占用）；
    2) **不能带 SO_REUSEADDR**：实测当别的进程绑在 0.0.0.0 时，带 REUSEADDR 探测 127.0.0.1
       会返回"可用"，然后真正启动时撞车（这正是把用户送到别人页面的根因）。宁可严格一点。
    地址族用 getaddrinfo 拿，保证与目标 host 匹配。
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for family, socktype, proto, _, sockaddr in infos:
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.bind(sockaddr)
        except OSError:
            return False
    return True


def find_free_port(host: str, start: int, tries: int = 20) -> int | None:
    for candidate in range(start, start + tries):
        if is_available(host, candidate):
            return candidate
    return None


def occupant(port: int) -> str | None:
    """尽力识别占用者（命令名 + PID）。拿不到就返回 None，不影响主流程。"""
    try:
        out = subprocess.run(  # noqa: S603
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line for line in out.splitlines()[1:] if line.strip()]
    if not lines:
        return None
    parts = lines[0].split()
    command = parts[0] if parts else "?"
    pid = parts[1] if len(parts) > 1 else "?"
    return f"{command} (PID {pid})"


def ensure_port(host: str, port: int, *, tries: int = 20) -> tuple[int, str | None]:
    """返回 (可用端口, 提示信息)。端口被占则自动向后找一个空闲端口。

    这里不是"静默换端口"——调用方必须把提示打印出来，用户得知道实际地址变了。
    """
    if is_available(host, port):
        return port, None
    holder = occupant(port)
    fallback = find_free_port(host, port + 1, tries)
    if fallback is None:
        raise OSError(f"端口 {port} 已被占用（{holder or '未知进程'}），且后续 {tries} 个端口也不可用")
    return fallback, f"端口 {port} 已被 {holder or '其他进程'} 占用，已改用 {fallback}"