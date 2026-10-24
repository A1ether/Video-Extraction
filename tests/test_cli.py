"""CLI 冒烟：命令能跑、错误渲染带 hint、退出码非 0。"""

from __future__ import annotations

from typer.testing import CliRunner

from vextract import registry
from vextract.cli import app
from vextract.errors import NoMedia

runner = CliRunner()


def test_platforms_lists_verified_matrix():
    result = runner.invoke(app, ["platforms"])
    assert result.exit_code == 0
    for platform in ("bilibili", "weibo", "kuaishou", "xhs", "douyin"):
        assert platform in result.stdout


def test_doctor_reports_environment():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "ffmpeg" in result.stdout
    assert "yt-dlp" in result.stdout


def test_info_renders_error_with_hint(monkeypatch):
    async def boom(text: str, *, quality: str = "best"):
        raise NoMedia("这条笔记是图文", hint="请用视频笔记", platform="xhs")

    monkeypatch.setattr(registry, "extract", boom)
    result = runner.invoke(app, ["info", "https://www.xiaohongshu.com/explore/1"])
    assert result.exit_code == 1
    assert "NO_MEDIA" in result.stdout
    assert "请用视频笔记" in result.stdout


def test_info_json_output(monkeypatch):
    from vextract.models import MediaInfo, Stream

    async def ok(text: str, *, quality: str = "best"):
        return MediaInfo(
            platform="weibo",
            media_id="1",
            source_url="u",
            title="标题",
            streams=[Stream(url="https://cdn/v.mp4", quality="hd")],
        )

    monkeypatch.setattr(registry, "extract", ok)
    result = runner.invoke(app, ["info", "https://m.weibo.cn/detail/1", "--json"])
    assert result.exit_code == 0
    assert '"platform": "weibo"' in result.stdout