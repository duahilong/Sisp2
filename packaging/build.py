"""一键打包：``python packaging/build.py``（单文件）或 ``--onedir``（目录版）。

步骤：确保图标存在 → 调 PyInstaller 按 ``packaging/Sisp2.spec`` 出单文件
exe → 打印产物大小。默认输出到 ``dist/``，构建中间物在 ``build/pyinstaller/``。

常用参数：
    --clean-only   只清理，不打包
    --no-clean     不清理缓存直接打包（快，但可能带旧模块）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = HERE / "Sisp2.spec"
ICON = HERE / "sisp2.ico"
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
TARGETS = ("Sisp2.exe",)
ONEDIR_TARGET = Path("Sisp2") / "Sisp2.exe"


def ensure_icon() -> Path:
    if not ICON.is_file():
        print(f"图标不存在，重新生成：{ICON}")
        from make_icon import build_ico

        build_ico(ICON)
    return ICON


def run_pyinstaller(clean: bool, onedir: bool = False) -> int:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
    ]
    if clean:
        command.append("--clean")
    print("执行：" + subprocess.list2cmdline(command))
    env = dict(os.environ)
    env["SISP2_BUILD_MODE"] = "onedir" if onedir else "onefile"
    return subprocess.call(command, cwd=str(ROOT), env=env)


def report(onedir: bool = False) -> bool:
    ok = True
    targets = [ONEDIR_TARGET] if onedir else list(TARGETS)
    for relative in targets:
        path = DIST / relative
        if not path.is_file():
            print(f"  缺失：{path}")
            ok = False
            continue
        if onedir:
            # 目录版：把 exe 同目录（含 _internal）的总大小算出来
            folder = path.parent
            total = sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())
            print(f"  目录版 Sisp2/  {total / 1024 / 1024:6.1f} MB  {folder}")
            print(f"    {path.name:12s} {path.stat().st_size / 1024 / 1024:6.1f} MB  {path}")
        else:
            print(f"  {path.name:14s} {path.stat().st_size / 1024 / 1024:6.1f} MB  {path}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 Sisp2 为单文件 exe")
    parser.add_argument("--clean-only", action="store_true", help="只清理构建缓存")
    parser.add_argument("--no-clean", action="store_true", help="跳过 PyInstaller --clean")
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="产出目录版 dist/Sisp2/（不打成单文件，启动快数倍，分发时整目录带走）",
    )
    args = parser.parse_args(argv)

    if args.clean_only:
        for path in (DIST, WORK, ROOT / "build" / "Sisp2"):
            shutil.rmtree(path, ignore_errors=True)
        print("已清理 dist/ 与 build/pyinstaller/")
        return 0

    ensure_icon()
    code = run_pyinstaller(clean=not args.no_clean, onedir=args.onedir)
    if code != 0:
        print(f"PyInstaller 失败，退出码 {code}")
        return code
    print("构建产物：")
    return 0 if report(onedir=args.onedir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
