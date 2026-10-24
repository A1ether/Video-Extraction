"""端口探测：这条链路的 bug 曾经把用户送到"要求登录 Claude"的别人家页面上。"""

from __future__ import annotations

import socket

import pytest

from vextract import ports


@pytest.fixture
def taken_port() -> int:
    """真的占住一个端口（而不是 mock），保证测试测的是真实行为。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def test_is_available_true_for_ephemeral_port(taken_port: int):
    # 取一个刚释放的端口：应该可用
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free = probe.getsockname()[1]
    probe.close()
    assert ports.is_available("127.0.0.1", free) is True
    assert ports.is_available("127.0.0.1", taken_port) is False


def test_find_free_port_skips_taken_port(taken_port: int):
    found = ports.find_free_port("127.0.0.1", taken_port, tries=5)
    assert found is not None
    assert found != taken_port
    assert ports.is_available("127.0.0.1", found)


def test_ensure_port_keeps_port_when_free():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free = probe.getsockname()[1]
    probe.close()
    chosen, notice = ports.ensure_port("127.0.0.1", free)
    assert chosen == free
    assert notice is None


def test_ensure_port_shifts_and_reports_notice(taken_port: int):
    chosen, notice = ports.ensure_port("127.0.0.1", taken_port, tries=5)
    assert chosen != taken_port
    assert notice and str(taken_port) in notice and str(chosen) in notice


def test_ensure_port_raises_when_nothing_free(taken_port: int, monkeypatch):
    monkeypatch.setattr(ports, "find_free_port", lambda *a, **k: None)
    with pytest.raises(OSError):
        ports.ensure_port("127.0.0.1", taken_port)


def test_occupant_is_best_effort(monkeypatch):
    monkeypatch.setattr(ports.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError))
    assert ports.occupant(1234) is None  # 没有 lsof 也不能崩


def test_occupant_parses_lsof_output(monkeypatch):
    class Result:
        stdout = "COMMAND   PID USER   FD   TYPE  DEVICE SIZE/OFF NODE NAME\nclaude-sc 2557 a1ether 25u IPv6 0x1 0t0 TCP [::1]:8000 (LISTEN)\n"

    monkeypatch.setattr(ports.subprocess, "run", lambda *a, **k: Result())
    assert ports.occupant(8000) == "claude-sc (PID 2557)"


def test_probe_does_not_use_so_reuseaddr():
    """别的进程绑在 0.0.0.0 时，带 SO_REUSEADDR 的探测会把 127.0.0.1 误判为可用。"""
    wild = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    wild.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    wild.bind(("0.0.0.0", 0))
    wild.listen(1)
    port = wild.getsockname()[1]
    try:
        assert ports.is_available("127.0.0.1", port) is False
    finally:
        wild.close()


def test_default_port_is_not_a_common_landgrab():
    """默认端口不该是 8000 这种常被别的东西占用的（本机实情）。"""
    assert ports.DEFAULT_PORT not in {80, 3000, 5000, 8000, 8080, 8888}