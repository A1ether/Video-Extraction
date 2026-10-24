"""页面内嵌 JSON 容错层——国内平台改版第一现场。"""

from __future__ import annotations

import json

import pytest

from vextract.errors import UpstreamChanged
from vextract.parsers import balanced_object, extract_js_object, sanitize_js_json


def test_sanitize_handles_platform_quirks():
    raw = '{"a":undefined,"b":new Map([]),"c":new Set(["x"]),"d":NaN,"e":[1,2,],}'
    parsed = json.loads(sanitize_js_json(raw))
    assert parsed == {"a": None, "b": {}, "c": [], "d": None, "e": [1, 2]}


def test_control_chars_including_c1_are_stripped():
    # 真实案例：小红书 SSR JSON 里出现过 U+0083，直接 json.loads 会报 ReaderError
    assert json.loads(sanitize_js_json('{"a":"\u0083x"}')) == {"a": "x"}
    assert json.loads(sanitize_js_json('{"a":"x\ufeffy"}')) == {"a": "xy"}


def test_balanced_object_skips_braces_inside_strings():
    text = 'x = {"s":"a}b{c","n":{"k":1}}; more'
    start = text.index("{")
    assert balanced_object(text, start) == '{"s":"a}b{c","n":{"k":1}}'


def test_extract_js_object_finds_state():
    html = '<script>window.__APOLLO_STATE__={"defaultClient":{"x":1}};</script>'
    assert extract_js_object(html, "window.__APOLLO_STATE__") == {"defaultClient": {"x": 1}}


def test_extract_js_object_reports_marker_change():
    with pytest.raises(UpstreamChanged) as err:
        extract_js_object("<html>nothing here</html>", "window.__APOLLO_STATE__")
    assert err.value.code == "UPSTREAM_CHANGED"
    assert "结构变了" in (err.value.hint or "")
    assert "改版" in err.value.render()


def test_balanced_object_reports_truncation():
    with pytest.raises(UpstreamChanged):
        balanced_object('{"a":1', 0)