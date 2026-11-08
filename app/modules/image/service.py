"""镜像探测（Ghost 版）：认 GHO 文件头、给出不可用类型的拒绝原因。

装机只支持 **Ghost 分区块镜像**（``.GHO`` / ``.GHS``）。以前这里还有两条路：

- ``dism /Get-WimInfo`` 读 WIM/ESD 索引（随"只用 Ghost"移除）；
- ``Mount-DiskImage`` 挂载 ISO 再取 ``sources\\install.wim``（同样移除）。

现在整个模块**不 spawn 任何子进程**，只读文件系统与前两个字节：
:func:`inspect_image` 校验单个文件（GHO 走 :func:`sniff_gho` 读文件头）。
镜像入口只有设置里的 A/B 两个槽位，没有“扫描目录挑一个”的旧路径。

计划期校验（``workflow._image_problems``）复用同一套判定，避免"界面说能用、
计划说不能用"的双口径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.modules.ghost.service import GHO_SUFFIXES, sniff_gho

# 不能直接还原的类型：保留这些元组只为了给出准确的拒绝原因。
ISO_SUFFIXES: tuple[str, ...] = (".iso",)
LEGACY_SUFFIXES: tuple[str, ...] = (".wim", ".esd", ".swm")

KIND_GHO = "gho"


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


def probe(path: str | Path) -> dict[str, Any]:
    """``/api/images`` 的单文件入口（路径必填）。"""
    return inspect_image(path)


# --------------------------------------------------------------- 系统版本槽位
# 设置页里固定两个槽位，各存一个 GHO。名字是用户可改的显示名（向导第 3 步的
# 按钮文字），所以槽位 id（A/B）与显示名分开存。
VERSION_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("A", "sysImageA", "sysImageALabel"),
    ("B", "sysImageB", "sysImageBLabel"),
)


def configured_versions(settings: Any) -> list[dict[str, str]]:
    """设置 → 可选系统版本列表（只含填了镜像路径的槽位）。

    ``label`` 空时退回 ``系统 A`` / ``系统 B``，保证向导上的按钮总有字。
    这里不做文件检查：调用方（预检查 / API）各自决定要不要 stat 文件。
    """
    data = settings if isinstance(settings, dict) else {}
    versions: list[dict[str, str]] = []
    for slot, path_key, label_key in VERSION_SLOTS:
        path = str(data.get(path_key) or "").strip()
        if not path:
            continue
        label = " ".join(str(data.get(label_key) or "").split()) or f"系统 {slot}"
        versions.append({"id": slot, "label": label, "path": str(Path(path).expanduser())})
    return versions


def find_version(settings: Any, path: str) -> dict[str, str] | None:
    """按镜像路径反查它属于哪个槽位（向导里回填时用）。"""
    wanted = str(Path(str(path)).expanduser())
    for version in configured_versions(settings):
        if version["path"] == wanted:
            return version
    return None


__all__ = [
    "GHO_SUFFIXES",
    "ISO_SUFFIXES",
    "KIND_GHO",
    "LEGACY_SUFFIXES",
    "VERSION_SLOTS",
    "ImageError",
    "configured_versions",
    "find_version",
    "inspect_image",
    "probe",
]
