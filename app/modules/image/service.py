"""镜像探测：列目录里的镜像文件、读 WIM/ESD 的版本列表。

界面里“系统安装 → 选择版本”这一步需要真实数据：

- ``sysIsoDir`` 指向的目录里有哪些 ``.wim/.esd/.iso``；
- 某个文件里有哪些索引（``Windows 11 专业版 / 家庭版 …``）。

两个来源都只有系统工具能做：``dism /Get-WimInfo`` 读索引，ISO 需要
``Mount-DiskImage`` 挂载后去 ``sources\\install.*`` 找镜像。

本模块把这两件事包起来，并保证：

- ISO 挂载后**一定**卸载（``finally``），不留下挂起的盘符；
- 没有任何写操作，不依赖 diskpart；
- 解析逻辑拆成纯函数（``parse_wim_info``），可以用样例文本直接测。
"""

from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = (".wim", ".esd", ".swm")
ISO_SUFFIXES = (".iso",)
LIST_SUFFIXES = IMAGE_SUFFIXES + ISO_SUFFIXES

DISM_TIMEOUT = 120.0

_INDEX_RE = re.compile(r"^\s*Index\s*:\s*(\d+)\s*$", re.IGNORECASE)
_FIELD_RE = re.compile(r"^\s*([A-Za-z ]+?)\s*:\s*(.*?)\s*$")


class ImageError(ValueError):
    """镜像探测失败时抛出的用户可读错误。"""


def _size_display(size_bytes: int | None) -> str:
    if not size_bytes:
        return "未知"
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def list_directory(directory: str | Path) -> dict[str, Any]:
    """列出目录里的镜像文件（只读）。"""
    base = Path(directory).expanduser()
    if not str(directory).strip():
        raise ImageError("尚未设置镜像目录（设置 → 系统文件 → 镜像目录）")
    if not base.is_dir():
        raise ImageError(f"镜像目录不存在：{base}")

    images: list[dict[str, Any]] = []
    try:
        entries = sorted(base.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ImageError(f"无法读取镜像目录：{exc}") from exc

    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in LIST_SUFFIXES:
            continue
        try:
            size_bytes = entry.stat().st_size
        except OSError:
            size_bytes = None
        images.append(
            {
                "path": str(entry),
                "name": entry.name,
                "suffix": entry.suffix.lower(),
                "kind": "iso" if entry.suffix.lower() in ISO_SUFFIXES else "wim",
                "size_bytes": size_bytes,
                "size_display": _size_display(size_bytes),
            }
        )
    return {"directory": str(base), "images": images, "exists": True}


def parse_wim_info(text: str) -> list[dict[str, Any]]:
    """解析 ``dism /Get-WimInfo`` 的输出。

    输出形如::

        Details for image : D:\\images\\win11.wim

        Index : 1
        Name : Windows 11 Pro
        Description : Windows 11 Pro
        Size : 4,921,234,567 bytes

    不同的 dism 版本字段顺序稳定但字段集合略有差异，因此按“遇到 Index
    就开一条记录，其余键值对挂到当前记录”的方式解析。
    """
    images: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in (text or "").splitlines():
        match = _INDEX_RE.match(raw_line)
        if match:
            if current is not None:
                images.append(current)
            current = {"index": int(match.group(1)), "name": "", "description": "", "size": ""}
            continue
        if current is None:
            continue
        field = _FIELD_RE.match(raw_line)
        if not field:
            continue
        key = field.group(1).strip().lower()
        value = field.group(2).strip()
        if not value:
            continue
        if key == "name":
            current["name"] = value
        elif key == "description":
            current["description"] = value
        elif key == "size":
            current["size"] = value
        elif key in ("edition", "architecture", "version", "languages"):
            current[key] = value
    if current is not None:
        images.append(current)
    return [image for image in images if image.get("index")]


class ImageProbe:
    """``dism`` 与 ISO 挂载的薄封装，全部依赖可注入以便测试。"""

    def __init__(
        self,
        *,
        dism: str | None = None,
        powershell: str | None = None,
        which: Callable[[str], str | None] | None = None,
        run: Callable[[list[str], float], tuple[int, str]] | None = None,
        mount: Callable[[str], dict[str, Any]] | None = None,
        unmount: Callable[[str], None] | None = None,
    ) -> None:
        self._which = which or shutil.which
        self.dism = dism or self._which("dism.exe")
        self.powershell = powershell or self._which("powershell.exe")
        self._run = run or _spawn
        self._mount = mount or self._mount_iso
        self._unmount = unmount or self._unmount_iso

    # ------------------------------------------------------------------ 入口
    def probe(self, path: str | None = None, *, directory: str | None = None) -> dict[str, Any]:
        """列目录（``directory``）或读镜像索引（``path``）。"""
        if path:
            return self.inspect(path)
        return list_directory(directory or "")

    def inspect(self, path: str) -> dict[str, Any]:
        target = Path(path).expanduser()
        if not target.exists():
            raise ImageError(f"镜像文件不存在：{target}")

        suffix = target.suffix.lower()
        if suffix in ISO_SUFFIXES:
            return self._inspect_iso(target)
        if suffix in IMAGE_SUFFIXES:
            return {
                "path": str(target),
                "name": target.name,
                "kind": "wim",
                "indexes": self.read_indexes(target),
                "mounted": False,
            }
        raise ImageError(f"不支持的镜像类型：{target.suffix or '无扩展名'}（支持 WIM/ESD/SWM/ISO）")

    # ------------------------------------------------------------------ WIM
    def read_indexes(self, path: str | Path) -> list[dict[str, Any]]:
        if not self.dism:
            raise ImageError("找不到 dism.exe，无法读取镜像索引")
        code, output = self._run(
            [self.dism, "/English", f"/Get-WimInfo", f"/WimFile:{path}"], DISM_TIMEOUT
        )
        if code != 0:
            raise ImageError(f"读取镜像索引失败（dism 退出码 {code}）：{output.strip()[:400]}")
        indexes = parse_wim_info(output)
        if not indexes:
            raise ImageError("dism 没有返回任何镜像索引，文件可能不是有效的 WIM/ESD")
        return indexes

    # ------------------------------------------------------------------ ISO
    def _inspect_iso(self, target: Path) -> dict[str, Any]:
        mounted = self._mount(str(target))
        try:
            drive = mounted.get("drive") or ""
            if not drive:
                raise ImageError("ISO 挂载成功但没有拿到盘符")
            source_dir = Path(drive) / "sources"
            candidates = [
                item
                for item in (
                    source_dir / "install.wim",
                    source_dir / "install.esd",
                    source_dir / "install.swm",
                )
                if item.is_file()
            ]
            if not candidates:
                raise ImageError(f"ISO 里没有 sources\\install.wim/esd/swm：{target.name}")
            chosen = candidates[0]
            return {
                "path": str(target),
                "name": target.name,
                "kind": "iso",
                "inner_path": str(chosen),
                "indexes": self.read_indexes(chosen),
                "mounted": True,
                "temporarily_mounted": True,
            }
        finally:
            self._unmount(str(target))

    def _mount_iso(self, path: str) -> dict[str, Any]:
        script = (
            f"$image = Mount-DiskImage -ImagePath '{path}' -PassThru;"
            "$volume = $image | Get-Volume;"
            "[PSCustomObject]@{ drive = $volume.DriveLetter } | ConvertTo-Json -Compress"
        )
        if not self.powershell:
            raise ImageError("找不到 powershell.exe，无法挂载 ISO")
        code, output = self._run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", script], 120.0
        )
        if code != 0:
            raise ImageError(f"挂载 ISO 失败：{output.strip()[:400]}")
        import json

        try:
            data = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise ImageError(f"挂载 ISO 后无法解析盘符：{output.strip()[:200]}") from exc
        drive = str(data.get("drive") or "").strip().upper()[:1]
        return {"drive": f"{drive}:" if drive else ""}

    def _unmount_iso(self, path: str) -> None:
        if not self.powershell:
            return
        script = f"Dismount-DiskImage -ImagePath '{path}'"
        try:
            self._run([self.powershell, "-NoProfile", "-NonInteractive", "-Command", script], 120.0)
        except Exception:  # noqa: BLE001 - 卸载失败不能覆盖原本的错误
            pass


def _spawn(command: list[str], timeout: float) -> tuple[int, str]:
    import subprocess

    try:
        completed = subprocess.run(  # noqa: S603 - 固定命令
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{timeout:.0f} 秒）"
    except OSError as exc:
        return 127, f"无法启动命令：{exc}"
    text = (completed.stdout or b"").decode("mbcs", errors="replace")
    text += (completed.stderr or b"").decode("mbcs", errors="replace")
    return completed.returncode, text


def find_default_image(images: Iterable[dict[str, Any]]) -> str:
    """从目录列表里挑一个默认镜像：优先 wim，其次 esd，最后 iso。"""
    items = list(images)
    for kind, suffix in (("wim", ".wim"), ("wim", ".esd"), ("iso", ".iso")):
        for item in items:
            if item.get("kind") == kind and str(item.get("suffix")) == suffix:
                return str(item.get("path") or "")
    return str(items[0].get("path") or "") if items else ""
