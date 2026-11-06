"""镜像目录探测（Ghost 版）：列目录里的 GHO、认文件头。

装机只支持 **Ghost 分区块镜像**（``.GHO`` / ``.GHS``）。以前这里还有两条路：

- ``dism /Get-WimInfo`` 读 WIM/ESD 索引（随"只用 Ghost"移除）；
- ``Mount-DiskImage`` 挂载 ISO 再取 ``sources\\install.wim``（同样移除）。

现在整个模块**不 spawn 任何子进程**，只读文件系统与前两个字节：

- :func:`list_directory` 列目录（GHO 可装；ISO/WIM/ESD/SWM 也列出来，
  但标成不可用，好让界面解释"为什么选不了"）；
- :func:`inspect_image` 校验单个文件（GHO 走 :func:`sniff_gho` 读文件头）。

计划期校验（``workflow._image_problems``）复用同一套判定，避免"界面说能用、
计划说不能用"的双口径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.modules.ghost.service import GHO_SUFFIXES, sniff_gho

# 能直接还原的类型 / 只是列出来给人看的历史类型
ISO_SUFFIXES: tuple[str, ...] = (".iso",)
LEGACY_SUFFIXES: tuple[str, ...] = (".wim", ".esd", ".swm")
LIST_SUFFIXES: tuple[str, ...] = GHO_SUFFIXES + ISO_SUFFIXES + LEGACY_SUFFIXES

KIND_GHO = "gho"
KIND_ISO = "iso"
KIND_LEGACY = "legacy"


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


def _kind_of(suffix: str) -> str:
    if suffix in GHO_SUFFIXES:
        return KIND_GHO
    if suffix in ISO_SUFFIXES:
        return KIND_ISO
    return KIND_LEGACY


def _note_of(kind: str) -> str:
    if kind == KIND_GHO:
        return "Ghost 分区块镜像，可直接还原"
    if kind == KIND_ISO:
        return "ISO 不能直接还原：请用封装好的 GHO"
    return "已不支持 dism 路线（WIM/ESD/SWM）：请使用 GHO"


def list_directory(directory: str | Path) -> dict[str, Any]:
    """列出目录里的镜像文件（只读，不 spawn）。"""
    if not str(directory).strip():
        raise ImageError("尚未设置镜像目录（设置 → 系统文件 → 镜像目录）")
    base = Path(directory).expanduser()
    if not base.is_dir():
        raise ImageError(f"镜像目录不存在：{base}")

    images: list[dict[str, Any]] = []
    try:
        entries = sorted(base.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ImageError(f"无法读取镜像目录：{exc}") from exc

    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        suffix = entry.suffix.lower()
        if suffix not in LIST_SUFFIXES:
            continue
        try:
            size_bytes = entry.stat().st_size
        except OSError:
            size_bytes = None
        kind = _kind_of(suffix)
        images.append(
            {
                "path": str(entry),
                "name": entry.name,
                "suffix": suffix,
                "kind": kind,
                "installable": kind == KIND_GHO,
                "note": _note_of(kind),
                "size_bytes": size_bytes,
                "size_display": _size_display(size_bytes),
            }
        )
    return {"directory": str(base), "images": images, "exists": True}


def inspect_image(path: str | Path) -> dict[str, Any]:
    """校验单个镜像文件，返回界面/计划期都要用的那一份信息。"""
    target = Path(str(path)).expanduser()
    suffix = target.suffix.lower()
    if suffix in GHO_SUFFIXES:
        sniffed = sniff_gho(target)
        return {
            "path": sniffed["path"],
            "name": sniffed.get("name") or target.name,
            "kind": KIND_GHO,
            "installable": bool(sniffed.get("ok")),
            "exists": bool(sniffed.get("exists")),
            "magic_ok": bool(sniffed.get("ok")),
            "reason": sniffed.get("reason", ""),
            "size_bytes": sniffed.get("size_bytes"),
            "size_display": _size_display(sniffed.get("size_bytes")),
            "block_index": sniffed.get("block_index", 1),
        }
    if suffix in ISO_SUFFIXES:
        raise ImageError(f"{target.name} 是 ISO：Ghost 只能还原 .GHO，请先封装成 GHO")
    if suffix in LEGACY_SUFFIXES:
        raise ImageError(
            f"不支持 {suffix}：dism 路线已移除，装机只接受 GHO/GHS（Ghost 分区块镜像）"
        )
    raise ImageError(
        f"不支持的镜像类型：{target.suffix or '无扩展名'}（只接受 GHO/GHS）"
    )


def probe(directory: str | Path | None = None, path: str | Path | None = None) -> dict[str, Any]:
    """``/api/images`` 的统一入口：给 ``path`` 就读单个文件，否则列目录。"""
    if path:
        return inspect_image(path)
    return list_directory(directory or "")


def find_default_image(images: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    """从目录列表里挑一个默认镜像：优先 .gho，其次 .ghs。"""
    items = list(images)
    for suffix in GHO_SUFFIXES:
        for item in items:
            if str(item.get("suffix")) == suffix and item.get("installable", True):
                return str(item.get("path") or "")
    return ""


__all__ = [
    "GHO_SUFFIXES",
    "ISO_SUFFIXES",
    "KIND_GHO",
    "KIND_ISO",
    "KIND_LEGACY",
    "LEGACY_SUFFIXES",
    "LIST_SUFFIXES",
    "ImageError",
    "find_default_image",
    "inspect_image",
    "list_directory",
    "probe",
]
