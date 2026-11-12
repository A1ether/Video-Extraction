#!/usr/bin/env python3
"""构建可拷到别的 Apple Silicon Mac 上直接运行的独立版 vextract.app。

结构：
- PyInstaller 只产出 onedir 的 `server/` 载荷（Python、依赖、前端、faster-whisper）；
- 外层 `.app` 由 Swift/WKWebView 原生外壳承载：启动内置服务并把界面显示在
  自己的窗口里，不跳浏览器；
- 本机 Homebrew 的 ffmpeg/ffprobe 及动态库内联进 `server/_internal/ffmpeg`，
  并改写为包内相对路径；
- ad-hoc 签名后打成 zip / dmg。

产物（build/standalone/dist/）：
  vextract.app
  vextract-<version>-macos-arm64.zip
  vextract-<version>-macos-arm64.dmg

注意：随包的 Homebrew ffmpeg 是 GPL-3.0-or-later。这个独立包适合个人本机/私下拷贝，
若要公开分发，应换成 LGPL 构建或改为要求系统自备 ffmpeg。
"""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "standalone"
DIST = BUILD / "dist"
WORK = BUILD / "work"
VENV = BUILD / "venv"
APP_NAME = "vextract.app"
BUNDLE_ID = "dev.a1ether.vextract"
SWIFT_SOURCE = ROOT / "scripts" / "macos_launcher.swift"
SYSTEM_PREFIXES = ("/usr/lib/", "/System/", "/Library/Apple/")


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    print("+", " ".join(str(part) for part in cmd))
    return subprocess.run(cmd, cwd=cwd, env=env, check=True)


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def build_venv() -> Path:
    if VENV.exists():
        shutil.rmtree(VENV)
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(VENV)
    # 用 uv.lock 的版本；只带基础依赖 + faster-whisper，避免把 mlx/torch 拖进来。
    run(["uv", "sync", "--extra", "transcribe", "--no-dev"], env=env)
    run(["uv", "pip", "install", "--python", str(VENV / "bin" / "python"), "pyinstaller>=6.11"])
    return VENV


def make_icon() -> Path | None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build_macos_app import build_icon  # noqa: PLC0415 — 复用同一个图标实现

    resources = BUILD / "icon"
    resources.mkdir(parents=True, exist_ok=True)
    if build_icon(resources):
        return resources / "AppIcon.icns"
    return None


def find_swiftc() -> str:
    swiftc = shutil.which("swiftc")
    if swiftc:
        return swiftc
    xcrun = shutil.which("xcrun")
    if xcrun:
        try:
            found = subprocess.run(
                [xcrun, "--find", "swiftc"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            found = ""
        if found and Path(found).exists():
            return found
    raise SystemExit("找不到 swiftc：先安装 Xcode Command Line Tools（xcode-select --install）")


def macho_dependencies(path: Path) -> list[str]:
    """otool -L 的依赖列表；dylib 的第一行是它自己的 LC_ID，要跳过。"""
    output = subprocess.run(["otool", "-L", str(path)], check=True, capture_output=True, text=True).stdout
    lines = [line.strip() for line in output.splitlines()[1:] if line.strip()]
    if path.suffix == ".dylib" and lines:
        lines = lines[1:]
    return [line.split(" (")[0] for line in lines]


def is_system(dep: str) -> bool:
    return dep.startswith(SYSTEM_PREFIXES)


def find_dependency(dep: str, current: Path, lib_dir: Path) -> Path | None:
    """把一条依赖解析成真实文件：绝对路径、@rpath、@loader_path 都试一遍。"""
    name = Path(dep).name
    if dep.startswith("/"):
        path = Path(dep)
        if path.exists():
            return path.resolve()
    elif dep.startswith("@loader_path/"):
        path = current.parent / dep.split("/", 1)[1]
        if path.exists():
            return path.resolve()
    elif dep.startswith("@rpath/"):
        name = dep.split("/", 1)[1]
    if (lib_dir / name).exists():
        return lib_dir / name

    bases = ["/opt/homebrew/lib", "/usr/local/lib"]
    bases += [str(p) for p in Path("/opt/homebrew/opt").glob("*/lib")] if Path("/opt/homebrew/opt").exists() else []
    for base in bases:
        candidate = Path(base) / name
        if candidate.exists():
            return candidate.resolve()
    return None


def bundle_ffmpeg(target_root: Path) -> None:
    """把 ffmpeg/ffprobe 和依赖库内联到 target_root/ffmpeg 下。

    target_root 是 server/_internal；服务端 setup_path 会优先用这里的 bin。
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("本机找不到 ffmpeg/ffprobe：先 brew install ffmpeg")

    root = target_root / "ffmpeg"
    bin_dir = root / "bin"
    lib_dir = root / "lib"
    bin_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)

    dest_ffmpeg = bin_dir / "ffmpeg"
    dest_ffprobe = bin_dir / "ffprobe"
    shutil.copy2(Path(ffmpeg).resolve(), dest_ffmpeg)
    shutil.copy2(Path(ffprobe).resolve(), dest_ffprobe)
    for binary in (dest_ffmpeg, dest_ffprobe):
        binary.chmod(0o755)

    # 关键：依赖里写的是符号链接名（libavcodec.63.dylib），而 resolve() 后是
    # 版本化文件名（libavcodec.63.1.102.dylib）。落盘必须用依赖里的名字，
    # 否则 install_name_tool 改完仍然指向一个不存在的路径。
    copied: dict[str, Path] = {}
    queue = [dest_ffmpeg, dest_ffprobe]
    while queue:
        current = queue.pop()
        for dep in macho_dependencies(current):
            if is_system(dep):
                continue
            name = Path(dep).name
            source = find_dependency(dep, current, lib_dir)
            if source is None:
                raise SystemExit(f"无法解析依赖：{dep}（来自 {current.name}）")
            if name in copied:
                if copied[name].resolve() != source.resolve():
                    raise SystemExit(f"同名依赖指向了不同文件：{name}\n  {copied[name]}\n  {source}")
                continue
            target = lib_dir / name
            if target.exists():
                continue
            shutil.copy2(source, target)
            target.chmod(0o755)
            copied[name] = source
            queue.append(target)

    # 改写引用：ffmpeg/ffprobe → @executable_path/../lib；dylib 之间 → @loader_path。
    for binary in (dest_ffmpeg, dest_ffprobe):
        for dep in macho_dependencies(binary):
            if is_system(dep):
                continue
            name = Path(dep).name
            if name in copied:
                run(
                    [
                        "install_name_tool",
                        "-change",
                        dep,
                        f"@executable_path/../lib/{name}",
                        str(binary),
                    ]
                )

    for lib in lib_dir.glob("*.dylib"):
        run(["install_name_tool", "-id", f"@rpath/{lib.name}", str(lib)])
        for dep in macho_dependencies(lib):
            if is_system(dep):
                continue
            name = Path(dep).name
            if name in copied and name != lib.name:
                run(["install_name_tool", "-change", dep, f"@loader_path/{name}", str(lib)])

    # 任何残留的 /opt/homebrew、/usr/local 依赖都会在目标机器上崩，直接判构建失败。
    leftovers: list[str] = []
    for path in (dest_ffmpeg, dest_ffprobe, *lib_dir.glob("*.dylib")):
        for dep in macho_dependencies(path):
            if is_system(dep):
                continue
            if not dep.startswith(("@executable_path/../lib/", "@loader_path/")):
                leftovers.append(f"{path.name} → {dep}")
    if leftovers:
        raise SystemExit("ffmpeg 动态库没有完全内联：\n  " + "\n  ".join(leftovers))

    for path in (dest_ffmpeg, dest_ffprobe, *lib_dir.glob("*.dylib")):
        run(["codesign", "--force", "--sign", "-", str(path)])

    clean_env = {"PATH": "/usr/bin:/bin"}
    for binary in (dest_ffmpeg, dest_ffprobe):
        result = subprocess.run([str(binary), "-version"], env=clean_env, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"随包 {binary.name} 跑不起来：\n{result.stderr[:800]}")
        print(f"  · {binary.name} OK：{result.stdout.splitlines()[0]}")


def write_plist(app: Path, version: str) -> None:
    info = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleDisplayName": "vextract",
        "CFBundleExecutable": "vextract",
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "vextract",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "LSArchitecturePriority": ["arm64"],
        "LSMinimumSystemVersion": "13.0",
        "LSMultipleInstancesProhibited": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    }
    with (app / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump(info, fh)


def create_native_app(server_dir: Path, version: str, icon: Path | None) -> Path:
    swiftc = find_swiftc()
    app = DIST / APP_NAME
    if app.exists():
        shutil.rmtree(app)

    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    print("== 复制服务载荷 ==")
    shutil.copytree(server_dir, resources / "server", symlinks=True)
    if icon:
        shutil.copy2(icon, resources / "AppIcon.icns")

    print("== 编译 Swift/WKWebView 外壳 ==")
    executable = macos / "vextract"
    run(
        [
            swiftc,
            "-swift-version",
            "5",
            "-O",
            "-target",
            "arm64-apple-macos13.0",
            "-framework",
            "AppKit",
            "-framework",
            "WebKit",
            str(SWIFT_SOURCE),
            "-o",
            str(executable),
        ]
    )
    executable.chmod(0o755)

    print("== 内联 ffmpeg ==")
    bundle_ffmpeg(resources / "server" / "_internal")

    write_plist(app, version)
    sign_app(app)
    return app


def sign_app(app: Path) -> None:
    run(["codesign", "--force", "--deep", "--sign", "-", str(app)])
    run(["codesign", "--verify", "--deep", "--strict", str(app)])
    print("  · 签名校验通过（ad-hoc）")


def make_zip(app: Path, version: str) -> Path:
    zip_path = DIST / f"vextract-{version}-macos-arm64.zip"
    zip_path.unlink(missing_ok=True)
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(zip_path)])
    return zip_path


def make_dmg(app: Path, version: str) -> Path:
    dmg_path = DIST / f"vextract-{version}-macos-arm64.dmg"
    dmg_path.unlink(missing_ok=True)
    run(
        [
            "hdiutil",
            "create",
            "-volname",
            "vextract",
            "-srcfolder",
            str(app),
            "-ov",
            "-format",
            "UDZO",
            str(dmg_path),
        ]
    )
    return dmg_path


def install(app: Path) -> Path:
    target = Path.home() / "Applications" / APP_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    run(["ditto", str(app), str(target)])
    return target


def clean_dist_app(app: Path) -> None:
    """删掉 dist/ 里的原始 .app 和 PyInstaller 的 server onedir，并注销 LaunchServices。"""
    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if lsregister.exists():
        subprocess.run([str(lsregister), "-u", str(app)], check=False, capture_output=True)
    shutil.rmtree(app, ignore_errors=True)
    for name in ("server", "vextract"):
        target = DIST / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="构建独立版 vextract.app（Apple Silicon）")
    parser.add_argument("--no-install", action="store_true", help="只产出 dist/，不复制到 ~/Applications")
    parser.add_argument("--no-zip", action="store_true", help="不生成 zip")
    parser.add_argument("--no-dmg", action="store_true", help="不生成 dmg")
    parser.add_argument(
        "--keep-app",
        action="store_true",
        help="保留 dist/ 里的原始 .app 与 server 载荷（默认打包后删除）",
    )
    args = parser.parse_args()

    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("这个构建脚本面向 macOS Apple Silicon（arm64）。")

    version = project_version()
    BUILD.mkdir(parents=True, exist_ok=True)
    print(f"== 构建 vextract {version}（独立版）==")
    build_venv()
    icon = make_icon()

    if DIST.exists():
        shutil.rmtree(DIST)
    if WORK.exists():
        shutil.rmtree(WORK)

    run(
        [
            str(VENV / "bin" / "pyinstaller"),
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST),
            "--workpath",
            str(WORK),
            str(ROOT / "scripts" / "vextract.spec"),
        ]
    )

    server_dir = DIST / "server"
    if not server_dir.exists():
        raise SystemExit(f"PyInstaller 没有产出 {server_dir}")
    app = create_native_app(server_dir, version, icon)

    zip_path = None if args.no_zip else make_zip(app, version)
    dmg_path = None if args.no_dmg else make_dmg(app, version)
    target = None if args.no_install else install(app)

    cleaned = False
    if not args.keep_app and (zip_path or dmg_path or target):
        clean_dist_app(app)
        cleaned = True

    print()
    print("完成：")
    if not cleaned:
        print(f"  app : {app}")
    if zip_path:
        print(f"  zip : {zip_path}")
    if dmg_path:
        print(f"  dmg : {dmg_path}")
    if target:
        print(f"  安装: {target}")
    if cleaned:
        print("  已清理 dist/ 里的原始 .app 与 server 载荷（要保留请加 --keep-app）")
    print()
    print("拷到别的 Apple Silicon Mac 后，解压/挂载 DMG，把 vextract.app 拖进「应用程序」即可。")
    print("首次打开若被 Gatekeeper 拦：右键 → 打开，或 `xattr -dr com.apple.quarantine /Applications/vextract.app`。")


if __name__ == "__main__":
    main()
