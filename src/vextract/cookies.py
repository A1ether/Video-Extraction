"""cookie 通道：只做"读用户给的 cookie"，不做登录、不碰浏览器数据库。

阶梯（docs/01 §6.3）：
  L0 无 cookie
  L1 ~/.config/vextract/cookies/<platform>.txt  (Netscape 格式，与 yt-dlp --cookies 通用)
  L2 由调用方传 --cookies-from-browser（转交 yt-dlp）

`vx cookies import` 负责把浏览器插件的导出落成 L1 文件：Cookie-Editor 的 JSON 导出、
或已经是 Netscape 的 cookies.txt 都吃。写下的文件只有 cookie 行 + 头行，权限 0600。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, NamedTuple

import httpx

COOKIE_DIR = Path(os.environ.get("VEXTRACT_HOME", Path.home() / ".config" / "vextract")) / "cookies"

PLATFORMS = ("douyin", "bilibili", "xhs", "kuaishou", "weibo")

NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
"""必须有：http.cookiejar 靠这行认 Netscape 格式，缺了 yt-dlp 会报 cookies 文件格式不对。"""

_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_BAD_VALUE_CHARS = ("\t", "\n", "\r", ";")
_HTTPONLY_PREFIX = "#HttpOnly_"


def cookie_path(platform: str, dest_dir: Path | None = None) -> Path:
    return (dest_dir or COOKIE_DIR) / f"{platform}.txt"


def cookie_file(platform: str) -> Path | None:
    path = cookie_path(platform)
    return path if path.exists() else None


def load_netscape(path: Path) -> list[tuple[str, str, str]]:
    """解析 Netscape cookie 文件 → [(domain, name, value)]。"""
    rows: list[tuple[str, str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            rows.append((parts[0], parts[5], parts[6]))
    return rows


def httpx_cookies(platform: str) -> httpx.Cookies | None:
    path = cookie_file(platform)
    if not path:
        return None
    jar = httpx.Cookies()
    for domain, name, value in load_netscape(path):
        jar.set(name, value, domain=domain.lstrip("."), path="/")
    return jar


def ytdlp_cookiefile(platform: str) -> str | None:
    path = cookie_file(platform)
    return str(path) if path else None


def describe(platform: str) -> str:
    path = cookie_file(platform)
    return f"已加载 {path}" if path else f"未找到 {COOKIE_DIR / f'{platform}.txt'}"


def summarize(platform: str) -> str:
    """只报条数与 cookie 名，绝不回显值。"""
    path = cookie_file(platform)
    if not path:
        return f"未配置（预期 {COOKIE_DIR / f'{platform}.txt'}）"
    rows = load_netscape(path)
    names = ", ".join(name for _, name, _ in rows)
    return f"{path} · {len(rows)} 条：{names}"


# --------------------------------------------------------------------------- 导入


class CookieRow(NamedTuple):
    domain: str
    path: str
    secure: bool
    expires: int
    name: str
    value: str

    def netscape_line(self) -> str:
        # includeSubdomains 用域名前导点判断——这是 Netscape 的既有约定，也决定 Cookie 头回给谁。
        flag = "TRUE" if self.domain.startswith(".") else "FALSE"
        return "\t".join(
            [
                self.domain,
                flag,
                self.path,
                "TRUE" if self.secure else "FALSE",
                str(self.expires),
                self.name,
                self.value,
            ]
        )


class ImportResult(NamedTuple):
    path: Path
    written: int
    skipped: list[str]
    """跳过原因（只写原因，不含 cookie 值）。"""


def parse_export(text: str) -> tuple[list[CookieRow], list[str]]:
    """自动识别 Cookie-Editor JSON / Netscape 文本 → 规范化 cookie 行 + 跳过原因。"""
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        entries = _json_entries(stripped)
    else:
        entries = _netscape_entries(text)

    rows: list[CookieRow] = []
    skipped: list[str] = []
    seen: dict[tuple[str, str, str], int] = {}
    for entry in entries:
        row, reason = _normalize(entry)
        if row is None:
            skipped.append(reason)
            continue
        key = (row.domain, row.path, row.name)
        if key in seen:  # 后出现的覆盖先出现的（浏览器里同名 cookie 以最新为准）
            rows[seen[key]] = row
            continue
        seen[key] = len(rows)
        rows.append(row)
    return rows, skipped


def import_export(text: str, platform: str, dest_dir: Path | None = None) -> ImportResult:
    """把浏览器导出写成 <platform>.txt（Netscape、0600）。"""
    if not _PLATFORM_RE.match(platform):
        raise ValueError(f"平台名不合法：{platform!r}（只用小写字母/数字/_/-）")
    rows, skipped = parse_export(text)
    if not rows:
        raise ValueError("导出里没有可用的 cookie 行（检查是否复制了完整的导出文件）")

    path = cookie_path(platform, dest_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    body = "\n".join([NETSCAPE_HEADER, *(row.netscape_line() for row in rows)]) + "\n"
    # 先建后写：避免"文件已存在时以宽松权限打开"的窗口期。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    finally:
        os.chmod(path, 0o600)
    return ImportResult(path=path, written=len(rows), skipped=skipped)


def _json_entries(text: str) -> list[Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）") from None
    if isinstance(payload, dict):
        payload = payload.get("cookies", payload.get("data", []))
    if not isinstance(payload, list):
        raise ValueError("JSON 顶层既不是数组，也没有 cookies/data 数组")
    return payload


def _netscape_entries(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith(_HTTPONLY_PREFIX):  # curl/Get cookies.txt 的 httpOnly 标记，去掉前缀
            line = line[len(_HTTPONLY_PREFIX) :]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        entries.append(
            {
                "domain": domain,
                "path": path,
                "secure": secure.strip().upper() == "TRUE",
                "expirationDate": expires,
                "name": name,
                "value": value,
            }
        )
    return entries


def _normalize(entry: Any) -> tuple[CookieRow | None, str]:
    if not isinstance(entry, dict):
        return None, "1 条不是对象（导出里混进了非 cookie 行）"
    name = str(entry.get("name") or "")
    if not name:
        # Cookie-Editor 会为站点出一行空 name 的占位项（如 value=douyin.com），必须丢。
        return None, "1 条没有 name（Cookie-Editor 的站点占位行）"
    domain = str(entry.get("domain") or "").strip()
    if not domain:
        return None, "1 条没有 domain"
    if any(ch in name or ch in (str(entry.get("value") or "")) for ch in _BAD_VALUE_CHARS):
        # Netscape 是制表符分隔，值里的制表符/换行/分号都存不下：宁可少一条，不写坏整个文件。
        return None, f"1 条值含制表符/换行/分号（Netscape 存不下）：{name}"

    value = "" if entry.get("value") is None else str(entry["value"])
    if entry.get("session"):
        expires = 0
    else:
        try:
            expires = max(0, int(float(entry.get("expirationDate") or 0)))
        except (TypeError, ValueError):
            expires = 0

    return (
        CookieRow(
            domain=domain,
            path=str(entry.get("path") or "/"),
            secure=bool(entry.get("secure")),
            expires=expires,
            name=name,
            value=value,
        ),
        "",
    )