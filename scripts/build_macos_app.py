#!/usr/bin/env python3
"""把 vextract 打成一个可双击的 macOS .app，并安装到 ~/Applications。

这不是 PyInstaller 式的"自包含二进制"：app 内的启动器直接使用项目现有的
`.venv/bin/vx`，因此本机转录（mlx-whisper / faster-whisper）等可选能力原样可用。
代价是 app 与项目目录绑定——移动项目后重新跑一次本脚本即可。

用法：
    uv run python scripts/build_macos_app.py
    uv run python scripts/build_macos_app.py --dest /Applications/vextract.app --launch

首次启动会在 ~/Library/Logs/vextract.log 留日志；Dock 里 Cmd+Q 即可停掉服务。
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "vextract"
BUNDLE_ID = "dev.a1ether.vextract"
DEFAULT_PORT = 8787
SCAN_PORTS = 13
"""启动时扫描 [port, port+12]：端口被占时 `vx serve` 会自动后移，这里要能找到它。"""


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def info_plist(version: str) -> dict:
    return {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "LSMinimumSystemVersion": "12.0",
        # 不禁止多实例：服务已在跑时，第二次双击会启动一个"扫描到实例→打开浏览器→退出"
        # 的短命进程，正好当"重新打开界面"用。
        "NSHighResolutionCapable": True,
    }


def launcher_script(port: int) -> str:
    root = str(ROOT)
    return f"""#!/bin/sh
# 由 scripts/build_macos_app.py 生成；项目移动后请重新生成。
set -u

ROOT={shell_quote(root)}
VX="${{ROOT}}/.venv/bin/vx"
PORT="${{VEXTRACT_PORT:-{port}}}"
LOG="${{HOME}}/Library/Logs/vextract.log"

# Finder 启动时 PATH 很短，补上 Homebrew，否则找不到 ffmpeg。
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="${{LANG:-zh_CN.UTF-8}}"

if [ ! -x "$VX" ]; then
  ALERT='display alert "vextract 启动失败" message "找不到 .venv/bin/vx。请先在项目目录运行 uv sync。" as critical'
  osascript -e "$ALERT" >/dev/null 2>&1 || true
  exit 1
fi

find_url() {{
  j=0
  while [ "$j" -lt {SCAN_PORTS} ]; do
    p=$((PORT + j))
    u="http://127.0.0.1:${{p}}/"
    if curl -fsS --connect-timeout 0.2 --max-time 0.5 "${{u}}health" 2>/dev/null | grep -q '"yt_dlp"'; then
      echo "$u"
      return 0
    fi
    j=$((j + 1))
  done
  return 1
}}

# 已经有服务在跑（端口可能因占用自动后移）：不再起第二个，直接把界面叫出来。
# 启动器本身不常驻——否则二次双击会被 LaunchServices 解释成 activate 旧进程，
# 既没有窗口也不会开浏览器。
if url=$(find_url); then
  open "$url"
  exit 0
fi

mkdir -p "$(dirname "$LOG")"
nohup "$VX" serve --port "$PORT" --no-open >>"$LOG" 2>&1 &

i=0
while [ "$i" -lt 40 ]; do
  if url=$(find_url); then
    open "$url"
    exit 0
  fi
  sleep 0.5
  i=$((i + 1))
done

ALERT='display alert "vextract 启动超时" message "服务未在 20 秒内启动，详见日志。" as critical'
osascript -e "$ALERT" >/dev/null 2>&1 || true
exit 1
"""


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#111c33"/>
      <stop offset="1" stop-color="#0a0f1d"/>
    </linearGradient>
  </defs>
  <rect width="1024" height="1024" rx="220" fill="url(#bg)"/>
  <circle cx="512" cy="512" r="300" fill="none" stroke="#38bdf8" stroke-width="36"/>
  <path d="M430 350 L430 674 L700 512 Z" fill="#ffffff"/>
</svg>
"""


def build_icon(resources: Path) -> bool:
    """用 macOS 自带的 qlmanage/sips/iconutil 生成 .icns；任何一步失败都不阻断打包。"""
    if not shutil.which("qlmanage") or not shutil.which("sips") or not shutil.which("iconutil"):
        print("  · 跳过图标：缺少 qlmanage/sips/iconutil")
        return False

    with tempfile.TemporaryDirectory(prefix="vextract-icon-") as tmp:
        tmpdir = Path(tmp)
        svg = tmpdir / "icon.svg"
        svg.write_text(ICON_SVG, encoding="utf-8")
        try:
            subprocess.run(
                ["qlmanage", "-t", "-s", "1024", "-o", str(tmpdir), str(svg)],
                check=True,
                capture_output=True,
            )
            png = tmpdir / "icon.svg.png"
            if not png.exists():
                raise RuntimeError("qlmanage 没有产出 PNG")

            iconset = tmpdir / "AppIcon.iconset"
            iconset.mkdir()
            # iconutil 要求完整的 iconset 名称集合（含 @2x），少一个都可能生成失败。
            variants = (
                (16, "16x16", ""),
                (32, "16x16", "@2x"),
                (32, "32x32", ""),
                (64, "32x32", "@2x"),
                (128, "128x128", ""),
                (256, "128x128", "@2x"),
                (256, "256x256", ""),
                (512, "256x256", "@2x"),
                (512, "512x512", ""),
                (1024, "512x512", "@2x"),
            )
            for size, base, suffix in variants:
                name = f"icon_{base}{suffix}.png"
                subprocess.run(
                    ["sips", "-z", str(size), str(size), str(png), "--out", str(iconset / name)],
                    check=True,
                    capture_output=True,
                )
            subprocess.run(
                ["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")],
                check=True,
                capture_output=True,
            )
            return True
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            print(f"  · 跳过图标：{exc}")
            return False


def sign(bundle: Path) -> None:
    if not shutil.which("codesign"):
        return
    result = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  · 已做 ad-hoc 签名")
    else:
        print(f"  · 签名失败（不影响本机运行）：{result.stderr.strip()}")


def build(dest: Path, port: int, *, icon: bool, launch: bool) -> Path:
    if sys.platform != "darwin":
        raise SystemExit("这个脚本只用于 macOS（生成 .app bundle）。")
    vx = ROOT / ".venv" / "bin" / "vx"
    if not vx.exists():
        raise SystemExit(f"找不到 {vx}\n先运行：uv sync --extra transcribe-mlx（或按需去掉 extra）")

    version = project_version()
    if dest.exists():
        print(f"重建：删除旧的 {dest}")
        shutil.rmtree(dest)

    contents = dest / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    with (contents / "Info.plist").open("wb") as fh:
        plistlib.dump(info_plist(version), fh)

    executable = macos / APP_NAME
    executable.write_text(launcher_script(port), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"打包 {APP_NAME} {version} → {dest}")
    if icon and build_icon(resources):
        print("  · 图标已生成")
    sign(dest)

    if launch:
        subprocess.run(["open", str(dest)], check=False)
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="把 vextract 打成 macOS .app")
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path.home() / "Applications" / f"{APP_NAME}.app",
        help="输出路径（默认 ~/Applications/vextract.app）",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"默认端口（{DEFAULT_PORT}）")
    parser.add_argument("--no-icon", action="store_true", help="不生成图标")
    parser.add_argument("--launch", action="store_true", help="打包完成后立即打开")
    args = parser.parse_args()

    dest = build(args.dest.expanduser(), args.port, icon=not args.no_icon, launch=args.launch)
    print()
    print("完成。双击打开，或：")
    print(f"  open {shell_quote(str(dest))}")
    print("日志：~/Library/Logs/vextract.log")


if __name__ == "__main__":
    main()
