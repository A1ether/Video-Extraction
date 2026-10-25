"""cookie 通道：浏览器导出 → L1 文件。全部离线，用 tmp 目录，绝不写用户真实 cookie 目录。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from vextract import cookies


@pytest.fixture(autouse=True)
def isolated_cookie_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cookies, "COOKIE_DIR", tmp_path / "cookies")
    yield tmp_path


def cookie_editor_export() -> str:
    """Cookie-Editor 的 JSON 形态：带一条空 name 的站点占位行。"""
    return json.dumps(
        [
            {
                "domain": ".douyin.com",
                "expirationDate": 1823151027.122168,
                "hostOnly": False,
                "httpOnly": False,
                "name": "UIFID",
                "path": "/",
                "secure": True,
                "session": False,
                "value": "abc.def",
            },
            {
                "domain": "www.douyin.com",
                "hostOnly": True,
                "httpOnly": False,
                "name": "",
                "path": "/",
                "secure": False,
                "session": True,
                "value": "douyin.com",
            },
            {
                "domain": "www.douyin.com",
                "expirationDate": 1793774946,
                "hostOnly": True,
                "httpOnly": True,
                "name": "sessionid",
                "path": "/",
                "secure": True,
                "session": False,
                "value": "sess-value",
            },
            {
                "domain": ".douyin.com",
                "hostOnly": False,
                "httpOnly": False,
                "name": "is_support_rtm_web_ts",
                "path": "/",
                "secure": False,
                "session": True,
                "value": "1",
            },
        ]
    )


# ---------- 解析 ----------
def test_parse_cookie_editor_export_drops_site_placeholder():
    rows, skipped = cookies.parse_export(cookie_editor_export())
    assert [row.name for row in rows] == ["UIFID", "sessionid", "is_support_rtm_web_ts"]
    assert len(skipped) == 1 and "没有 name" in skipped[0]


def test_parse_keeps_leading_dot_and_flags():
    rows, _ = cookies.parse_export(cookie_editor_export())
    lines = {row.name: row.netscape_line().split("\t") for row in rows}
    # 前导点 → includeSubdomains=TRUE（决定 Cookie 头回给谁）
    assert lines["UIFID"][0] == ".douyin.com" and lines["UIFID"][1] == "TRUE"
    assert lines["sessionid"][0] == "www.douyin.com" and lines["sessionid"][1] == "FALSE"
    assert lines["sessionid"][3] == "TRUE"  # secure
    assert lines["UIFID"][4] == "1823151027"  # 浮点 expirationDate 收敛成秒
    assert lines["is_support_rtm_web_ts"][4] == "0"  # session cookie → 0


def test_parse_skips_values_netscape_cannot_hold():
    payload = [
        {"domain": ".x.com", "name": "tabbed", "value": "a\tb"},
        {"domain": ".x.com", "name": "wrapped", "value": "a\nb"},
        {"domain": ".x.com", "name": "semi", "value": "a;b"},
        {"domain": ".x.com", "name": "ok", "value": "a%3Bb"},
    ]
    rows, skipped = cookies.parse_export(json.dumps(payload))
    assert [row.name for row in rows] == ["ok"]
    assert len(skipped) == 3
    assert "tabbed" in skipped[0] and "a\tb" not in skipped[0]  # 只报原因与名字，不回显值


def test_parse_accepts_netscape_input_with_httponly_prefix():
    text = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            "# 导出时间注释",
            "#HttpOnly_.douyin.com\tTRUE\t/\tTRUE\t1796232116\tttwid\tttwid-value",
            "",
            "www.douyin.com\tFALSE\t/\tFALSE\t1793774946\ts_v_web_id\tverify_abc",
        ]
    )
    rows, skipped = cookies.parse_export(text)
    assert [row.name for row in rows] == ["ttwid", "s_v_web_id"]
    assert rows[0].domain == ".douyin.com"  # httpOnly 前缀必须去掉，否则我们自己的读取器会当注释跳过
    assert skipped == []


def test_parse_dedupes_same_cookie_keeping_last():
    payload = [
        {"domain": ".x.com", "path": "/", "name": "n", "value": "old"},
        {"domain": ".x.com", "path": "/", "name": "n", "value": "new"},
    ]
    rows, _ = cookies.parse_export(json.dumps(payload))
    assert len(rows) == 1 and rows[0].value == "new"


def test_parse_rejects_garbage():
    with pytest.raises(ValueError, match="不是合法 JSON"):
        cookies.parse_export("{ nope")
    # 既不是 JSON 也不是 Netscape → 解析出 0 行；真正的拒绝发生在落盘那一步
    assert cookies.parse_export("这不是导出文件，只是一段话") == ([], [])
    with pytest.raises(ValueError, match="没有可用的 cookie 行"):
        cookies.import_export("这不是导出文件，只是一段话", "douyin")


# ---------- 落盘 ----------
def test_import_writes_netscape_file_with_600(tmp_path: Path):
    result = cookies.import_export(cookie_editor_export(), "douyin")
    assert result.written == 3 and result.path.name == "douyin.txt"

    text = result.path.read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File\n")  # yt-dlp 靠这行认格式
    assert text.count("\n") == 4  # 头 + 3 条，没有多余空行
    mode = stat.S_IMODE(result.path.stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert stat.S_IMODE(result.path.parent.stat().st_mode) == 0o700
    assert text.count("sess-value") == 1  # 值只出现在它那一行里


def test_imported_file_is_readable_by_both_readers():
    """同一个文件，yt-dlp（Netscape 行）和我们的 httpx 路径都要能读。"""
    result = cookies.import_export(cookie_editor_export(), "douyin")
    rows = cookies.load_netscape(result.path)
    assert [name for _, name, _ in rows] == ["UIFID", "sessionid", "is_support_rtm_web_ts"]

    assert cookies.ytdlp_cookiefile("douyin") == str(result.path)
    jar = cookies.httpx_cookies("douyin")
    assert jar is not None
    assert jar.get("sessionid", domain="www.douyin.com") == "sess-value"


def test_import_rejects_platform_traversal(tmp_path: Path):
    with pytest.raises(ValueError, match="平台名不合法"):
        cookies.import_export(cookie_editor_export(), "../../etc/passwd")
    assert list(tmp_path.rglob("*.txt")) == []


# ---------- 台账 ----------
def test_describe_and_summarize():
    assert "未找到" in cookies.describe("douyin")
    assert "未配置" in cookies.summarize("douyin")

    cookies.import_export(cookie_editor_export(), "douyin")
    assert "已加载" in cookies.describe("douyin")
    summary = cookies.summarize("douyin")
    assert "3 条" in summary and "sessionid" in summary
    assert "sess-value" not in summary  # show 只列名字


def test_import_overwrites_previous_export():
    cookies.import_export(cookie_editor_export(), "douyin")
    single = json.dumps([{"domain": ".douyin.com", "name": "ttwid", "value": "v"}])
    result = cookies.import_export(single, "douyin")
    assert result.written == 1
    assert cookies.load_netscape(result.path) == [(".douyin.com", "ttwid", "v")]


def test_cookie_path_roots_at_cookie_dir():
    """导入和读取必须落在同一处，否则"导入成功但用不上"。"""
    assert cookies.cookie_path("douyin").parent == cookies.COOKIE_DIR
    assert cookies.cookie_path("douyin").name == "douyin.txt"


def test_directories_use_env_home(monkeypatch, tmp_path: Path):
    import importlib

    monkeypatch.setenv("VEXTRACT_HOME", str(tmp_path / "custom"))
    reloaded = importlib.reload(cookies)
    try:
        expected = tmp_path / "custom" / "cookies"
        assert expected == reloaded.COOKIE_DIR
        assert reloaded.cookie_path("douyin") == expected / "douyin.txt"
    finally:
        monkeypatch.delenv("VEXTRACT_HOME")
        importlib.reload(cookies)


def test_file_mode_is_0600_even_if_loose_file_existed():
    """先建一个 0644 的同名文件，导入后必须是 0600（chmod 兜底）。"""
    path = cookies.cookie_path("weibo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old", encoding="utf-8")
    os.chmod(path, 0o644)

    cookies.import_export(json.dumps([{"domain": ".weibo.com", "name": "n", "value": "v"}]), "weibo")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600