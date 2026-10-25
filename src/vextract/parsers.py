"""页面内嵌 JSON 的健壮提取。

国内平台的 SSR 状态对象几乎都不是合法 JSON：会出现 `undefined`、`new Map([])`、
`new Set([])`、`NaN`、控制字符、尾逗号。直接 json.loads 一定失败，
这正是 XHS-Downloader issue #497 / #493 那类"平台一改版就全挂"的根因。
这里把容错集中到一处，平台改版时只改这个文件。
"""

from __future__ import annotations

import json
import re

from .errors import NoMedia, UpstreamChanged

_UNDEFINED = re.compile(r"\bundefined\b")
_NEW_MAP = re.compile(r"new Map\(\s*\[[^\]]*\]\s*\)|new Map\(\s*\)")
_NEW_SET = re.compile(r"new Set\(\s*\[[^\]]*\]\s*\)|new Set\(\s*\)")
_NAN = re.compile(r"\bNaN\b|\b-?Infinity\b")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u0080-\u009f\ufeff]")


def sanitize_js_json(text: str) -> str:
    text = _CTRL.sub("", text)
    text = _NEW_MAP.sub("{}", text)
    text = _NEW_SET.sub("[]", text)
    text = _UNDEFINED.sub("null", text)
    text = _NAN.sub("null", text)
    return _TRAILING_COMMA.sub(r"\1", text)


_IMAGE_EXT = ("jpg", "jpeg", "png", "webp", "gif", "heic", "avif")


def ext_of_url(url: str, default: str = "jpg") -> str:
    """从图片 URL 猜扩展名。

    直链常带查询串（`?imageView2/2/w/1080`）或裁切参数，所以只看路径部分的最后一段；
    猜不出来时给 default——图集是按 `01.jpg` 这种名字落盘的，扩展名错会让用户打不开。
    """
    path = url.split("?")[0].split("#")[0]
    tail = path.rsplit("/", 1)[-1]
    if "." not in tail:
        return default
    suffix = tail.rsplit(".", 1)[-1].lower()
    return suffix if suffix in _IMAGE_EXT else default


def balanced_object(text: str, start: int) -> str:
    """从 start 处的 `{` 开始做括号配对，跳过字符串字面量。"""
    depth = 0
    in_str: str | None = None
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in "\"'":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start: i + 1]
    raise UpstreamChanged("页面内嵌 JSON 不完整（可能被截断）", hint="稍后重试；持续失败请反馈该链接")


def extract_js_object(html: str, marker: str) -> dict:
    """按标记定位并解析页面内嵌状态对象。marker 不支持正则，纯字符串查找更快更稳。"""
    idx = html.find(marker)
    if idx < 0:
        raise UpstreamChanged(
            f"页面里找不到 {marker}（平台可能已改版）",
            hint="这通常意味着该平台前端结构变了；请升级本项目或反馈该链接",
            detail=f"marker={marker}",
        )
    brace = html.find("{", idx)
    if brace < 0:
        raise UpstreamChanged(f"{marker} 后没有对象字面量", hint="反馈该链接")
    raw = balanced_object(html, brace)
    try:
        return json.loads(sanitize_js_json(raw))
    except json.JSONDecodeError as exc:
        raise UpstreamChanged(
            "页面内嵌 JSON 解析失败",
            hint="平台字段结构变更，请反馈该链接（附 --debug）",
            detail=f"{exc} @ {raw[:120]}",
        ) from exc


def deep_find(node: object, predicate, depth: int = 0):
    """深度遍历找第一个满足 predicate 的值。"""
    if depth > 12:
        return None
    if predicate(node):
        return node
    if isinstance(node, dict):
        for value in node.values():
            found = deep_find(value, predicate, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = deep_find(value, predicate, depth + 1)
            if found is not None:
                return found
    return None


def first_key(node: dict, *names: str):
    for name in names:
        value = node.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def require(condition: object, message: str, *, hint: str, platform: str) -> None:
    if not condition:
        raise NoMedia(message, hint=hint, platform=platform)