"""引擎适配层的几条硬契约（全部离线，用假 yt_dlp 模块）。

最重要的一条：**yt-dlp 不得改写用户的 cookie 文件**。它的 cookie jar 在退出时会 save()，
实测一次运行就把 70 条削成 66 条（丢掉 sdk_source_info / bit_env 之类它不认识的条目）。
所以 engine 交给它的必须是临时副本。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from vextract import engine


@pytest.fixture
def fake_ytdlp(monkeypatch):
    """装一个假 yt_dlp：记录收到的 opts，并模拟"退出时改写 cookiefile"。"""
    state: dict[str, object] = {}

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            state["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            # 真实 yt-dlp 的行为：close() 时把 jar save 回 cookiefile
            path = self.opts.get("cookiefile")
            if path:
                Path(path).write_text("# Netscape HTTP Cookie File\n# rewritten by yt-dlp\n", encoding="utf-8")
            return False

        def extract_info(self, url, download=False):
            return {"id": "x", "title": "t", "formats": []}

        def sanitize_info(self, info):
            return info

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYDL  # type: ignore[attr-defined]
    module.version = types.SimpleNamespace(__version__="0.0")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return state


def make_cookie_file(path: Path) -> str:
    body = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            ".douyin.com\tTRUE\t/\tTRUE\t1823151027\tUIFID\t" + "a" * 320,
            ".douyin.com\tTRUE\t/\tTRUE\t1823151027\tbit_env\t" + "b" * 120,
        ]
    ) + "\n"
    path.write_text(body, encoding="utf-8")
    return body


def test_engine_never_rewrites_the_users_cookie_file(fake_ytdlp, tmp_path: Path):
    jar = tmp_path / "douyin.txt"
    original = make_cookie_file(jar)

    engine._extract_sync("https://example/v/1", str(jar), None)

    assert jar.read_text(encoding="utf-8") == original, "用户 cookie 文件被 yt-dlp 改写了"
    assert fake_ytdlp["opts"]["cookiefile"] != str(jar), "应当交给 yt-dlp 一份副本"


def test_engine_cleans_up_the_shadow_copy(fake_ytdlp, tmp_path: Path):
    jar = tmp_path / "douyin.txt"
    make_cookie_file(jar)

    engine._extract_sync("https://example/v/1", str(jar), None)
    shadow = Path(str(fake_ytdlp["opts"]["cookiefile"]))
    assert not shadow.exists(), "临时副本必须删掉（里面有 cookie）"


def test_shadow_copy_is_private(fake_ytdlp, tmp_path: Path):
    jar = tmp_path / "douyin.txt"
    make_cookie_file(jar)
    shadow_path, shadow = engine._shadow_cookiefile(str(jar))
    try:
        assert shadow.stat().st_mode & 0o777 == 0o600
        assert Path(shadow_path).read_text(encoding="utf-8") == jar.read_text(encoding="utf-8")
    finally:
        shadow.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("raw", "expect_hint_fragment"),
    [
        ("ERROR: Fresh cookies (not necessarily logged in) are needed", "cookie"),
        ("ERROR: HTTP Error 403: Forbidden", "cookie"),
        ("ERROR: Unsupported URL: https://x", "vx platforms"),
        ("ERROR: No video formats found!", "视频"),
    ],
)
def test_error_translation_is_actionable(raw: str, expect_hint_fragment: str):
    """平台原始报错绝不直接抛给用户：每条都要翻译成 code + 可执行 hint。"""
    translated = engine._translate(Exception(raw), "https://x")
    assert translated.code != "VEXTRACT_ERROR", raw
    assert expect_hint_fragment in (translated.hint or "")
    assert translated.detail  # 原文留在 detail，便于排查