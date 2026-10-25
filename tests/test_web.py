"""前端一致性检查（纯离线）：HTML/JS/CSS 三件套不漂移。

这类测试的价值在于：改了后端接口名或 HTML id，却没改前端，会在 CI/本地立刻炸，
而不是等到打开浏览器才发现白屏。
"""

from __future__ import annotations

import re
from pathlib import Path

import vextract

WEB = Path(vextract.__file__).parent / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "styles.css").read_text(encoding="utf-8")


def _normalize(path: str) -> str:
    """把 /tasks/{task_id}/file 与 /tasks/${task.id}/file 归一成同一形状。"""
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", "", path)   # JS 模板占位要先去掉，否则会留下孤立的 $
    path = re.sub(r"\{[^}]*\}", "", path)     # FastAPI 路由占位
    return re.sub(r"/+", "/", path).rstrip("/") or "/"


def _routes() -> set[tuple[str, str]]:
    from vextract.api.app import app

    out: set[tuple[str, str]] = set()
    for route in app.routes:
        for method in getattr(route, "methods", None) or []:
            out.add((_normalize(route.path), method))
    return out


def test_frontend_assets_exist():
    for name in ("index.html", "app.js", "styles.css"):
        assert (WEB / name).exists(), f"缺少 {name}"


def test_index_has_no_external_resources():
    """离线可用：不能依赖 CDN（用户可能在国内代理环境）。"""
    assert "//cdn" not in INDEX
    assert not re.search(r'(?:src|href)="https?://', INDEX)


def test_index_ids_used_by_js_all_exist():
    used = set(re.findall(r'\$\("#([a-zA-Z0-9_-]+)"\)', JS))
    used |= set(re.findall(r'\$\$\(\s*"#([a-zA-Z0-9_-]+)"', JS))
    declared = set(re.findall(r'id="([^"]+)"', INDEX))
    assert used, "JS 里应该至少引用了若干 DOM id"
    missing = sorted(used - declared)
    assert not missing, f"JS 引用了 index.html 里不存在的 id：{missing}"


def test_data_tab_targets_have_matching_panels():
    tabs = set(re.findall(r'data-tab="([^"]+)"', INDEX))
    panels = set(re.findall(r'id="panel-([^"]+)"', INDEX))
    assert tabs == panels, f"tab 与 panel 不匹配：tabs={tabs} panels={panels}"


def test_every_api_call_matches_a_real_route():
    routes = _routes()
    calls = []
    for match in re.finditer(r'api\(\s*[`"]([^`"]+)[`"]', JS):
        path = match.group(1)
        window = JS[match.end(): match.end() + 200]
        method = re.search(r'method:\s*"([A-Z]+)"', window)
        calls.append((_normalize(path), method.group(1) if method else "GET"))

    assert calls, "app.js 里应该调用了若干接口"
    for path, method in calls:
        assert (path, method) in routes, f"前端调用了不存在的接口：{method} {path}"


def test_player_and_download_urls_hit_the_file_route():
    routes = {(path, method) for path, method in _routes() if method == "GET"}
    targets = set(re.findall(r'[`"](/tasks/\$\{task\.id\}/file[^`"]*)[`"]', JS))
    assert targets, "任务卡片应通过 /tasks/{id}/file 播放与下载"
    for target in targets:
        assert (_normalize(target), "GET") in routes


def test_cover_goes_through_image_proxy():
    assert "proxy/image?url=" in JS or "/proxy/image" in JS
    assert "/proxy/image" in (WEB / "app.js").read_text(encoding="utf-8")


def test_sse_endpoint_used_by_frontend_exists():
    assert "/events" in JS
    assert (_normalize("/events"), "GET") in _routes()


def test_css_defines_core_classes():
    for selector in (".card", ".btn", ".progress", ".task", ".toast", ".pill", ".panel"):
        assert selector in CSS, f"styles.css 缺少 {selector}"


def test_streams_default_to_highest_rank_in_js():
    """默认选中最高清晰度视频轨，而不是数组第一个。"""
    assert "audio_only" in JS
    assert "rank" in JS


# ---------- 产物模式（音频 / 图集 / 转录）----------
def test_mode_selector_offers_all_three_modes():
    assert "dl-mode" in JS
    for mode in ("video", "audio", "images"):
        assert f'value: "{mode}"' in JS, f"缺 {mode} 模式选项"
    assert "isImagePost" in JS, "图文作品要能自动切到图集模式"


def test_audio_controls_and_transcribe_toggle_exist():
    for token in ("audio_format", "transcript_format", "transcribe", "audio_formats", "transcript_formats"):
        assert token in JS, f"前端没接上 {token}"
    assert "asrReady" in JS, "没装 ASR 依赖时转录勾选要禁用"


def test_settings_panel_binds_audio_and_asr_fields():
    for element in ("set-audio-format", "set-transcript-format", "set-asr-model", "set-asr-language", "set-embed-tags", "asr-status"):
        assert f'id="{element}"' in INDEX, f"设置页缺 {element}"


def test_artifact_urls_match_a_real_route():
    routes = {(path, "GET") for path, method in _routes() if method == "GET"}
    assert "/tasks/${task.id}/artifact?index=" in JS, "图集/文字稿要通过 /tasks/{id}/artifact 取"
    assert (_normalize("/tasks/{task_id}/artifact"), "GET") in routes, "后端缺少 artifact 路由"


def test_artifact_route_is_not_confused_with_file_route():
    """图集任务的 path 是目录，/file 会 409——前端必须用 /artifact 拿单张。"""
    assert "isImageTask" in JS
    assert "artifact?index=" in JS


def test_processing_state_is_labelled():
    assert "processing" in JS, "转码/转录阶段要有自己的状态标签"
    assert "处理中" in JS