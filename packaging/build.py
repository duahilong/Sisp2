"""一键打包：``python packaging/build.py``。

步骤：确保图标存在 → 调 PyInstaller 按 ``packaging/Sisp2.spec`` 出单文件
exe → 打印产物大小。默认输出到 ``dist/``，构建中间物在 ``build/pyinstaller/``。

常用参数：
    --clean-only   只清理，不打包
    --no-clean     不清理缓存直接打包（快，但可能带旧模块）
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = HERE / "Sisp2.spec"
BASE_VERSION_FILE = ROOT / "app" / "version.py"
VERSION_INFO_FILE = HERE / "version_info.txt"
ICON = HERE / "sisp2.ico"
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
TARGETS = ("Sisp2.exe",)


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """把基础版或 ``X.Y.Z-beta.N`` 转成 Windows 四段数字版本。"""
    match = re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.(0|[1-9]\d*))?",
        version,
    )
    if match is None:
        raise ValueError(f"不支持的版本号：{version!r}，应为 X.Y.Z 或 X.Y.Z-beta.N")
    numbers = tuple(int(part or 0) for part in match.groups())
    if any(part > 65535 for part in numbers):
        raise ValueError("Windows 数值版本的每一段都不能超过 65535")
    return numbers


def replace_app_version(text: str, version: str) -> str:
    """只替换基础版本模块的赋值行，供构建期临时注入。"""
    version_tuple(version)
    result, count = re.subn(
        r'(?m)^__version__ = "[^"]+"[ \t]*$',
        f'__version__ = "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise ValueError("app/version.py 中必须恰好有一个 __version__ 赋值")
    return result


def _replace_one(text: str, pattern: str, replacement: str) -> str:
    result, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        raise ValueError(f"Windows 版本资源缺少预期字段：{pattern}")
    return result


def render_version_info(template: str, version: str) -> str:
    """保留版权等元数据，只替换 Windows 数值版本与显示版本。"""
    numbers = version_tuple(version)
    text = _replace_one(template, r"filevers=\([^)]*\)", f"filevers={numbers}")
    text = _replace_one(text, r"prodvers=\([^)]*\)", f"prodvers={numbers}")
    for field in ("FileVersion", "ProductVersion"):
        text = _replace_one(
            text,
            rf"StringStruct\('{field}', '[^']*'\)",
            f"StringStruct('{field}', '{version}')",
        )
    return text


@contextmanager
def injected_version(version: str | None) -> Iterator[None]:
    """构建期间注入完整版本；无论成功与否都恢复仓库中的基础版本。"""
    if version is None:
        yield
        return
    version_tuple(version)
    paths = (BASE_VERSION_FILE, VERSION_INFO_FILE)
    originals = {path: path.read_bytes() for path in paths}
    try:
        BASE_VERSION_FILE.write_text(
            replace_app_version(BASE_VERSION_FILE.read_text(encoding="utf-8"), version),
            encoding="utf-8",
        )
        VERSION_INFO_FILE.write_text(
            render_version_info(VERSION_INFO_FILE.read_text(encoding="utf-8"), version),
            encoding="utf-8",
        )
        yield
    finally:
        for path, content in originals.items():
            path.write_bytes(content)


def ensure_icon() -> Path:
    if not ICON.is_file():
        print(f"图标不存在，重新生成：{ICON}")
        from make_icon import build_ico

        build_ico(ICON)
    return ICON


def run_pyinstaller(clean: bool) -> int:
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
    return subprocess.call(command, cwd=str(ROOT))


def report() -> bool:
    ok = True
    for name in TARGETS:
        path = DIST / name
        if path.is_file():
            print(f"  {name:14s} {path.stat().st_size / 1024 / 1024:6.1f} MB  {path}")
        else:
            print(f"  缺失：{path}")
            ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 Sisp2 为单文件 exe")
    parser.add_argument("--clean-only", action="store_true", help="只清理构建缓存")
    parser.add_argument("--no-clean", action="store_true", help="跳过 PyInstaller --clean")
    parser.add_argument("--version", help="构建期注入完整版本，例如 1.5.0-beta.8")
    args = parser.parse_args(argv)
    if args.version is not None:
        try:
            version_tuple(args.version)
        except ValueError as exc:
            parser.error(str(exc))

    if args.clean_only:
        for path in (DIST, WORK, ROOT / "build" / "Sisp2"):
            shutil.rmtree(path, ignore_errors=True)
        print("已清理 dist/ 与 build/pyinstaller/")
        return 0

    ensure_icon()
    print(f"构建版本：{args.version or '源码基础版本'}")
    # exe 冻结后 app/version.py 才会被读取；注入不能等构建结束才执行。
    with injected_version(args.version):
        code = run_pyinstaller(clean=not args.no_clean)
    if code != 0:
        print(f"PyInstaller 失败，退出码 {code}")
        return code
    print("构建产物：")
    return 0 if report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
