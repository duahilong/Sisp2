"""引导目标：磁盘号 + EFI 盘符 + Windows 盘符 + 引导源目录。

写引导（bcdboot）与校验（查文件）用的是同一份目标，所以它必须只有一个来源。
以前这两半各自从 ``PhaseContext`` 里挖 ``letters["efi"]`` / ``letters["windows"]``
与 ``options["boot_source"]``，盘符缺失时的报错也各写一遍；现在只有
``BootTarget.from_context()`` 认识上下文，其余代码只吃这个不可变对象。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class BootTargetError(ValueError):
    """盘符 / 引导源不完整，无法写引导（属于"现在就不能做"，不是命令失败）。"""


def _letter(value: Any) -> str:
    return str(value or "").strip().upper()[:1]


@dataclass(frozen=True)
class BootTarget:
    """一次引导写入的全部入参（不可变，可安全跨线程传递）。"""

    disk_number: int
    efi_letter: str
    windows_letter: str
    source: str
    #: 引导源是否来自界面设置（``sysBoot``）：用于在日志里说明来源。
    source_from_settings: bool = False

    @property
    def efi_root(self) -> str:
        return f"{self.efi_letter}:\\"

    @property
    def windows_root(self) -> str:
        return f"{self.windows_letter}:\\"

    @classmethod
    def from_context(cls, ctx: Any) -> BootTarget:
        """从阶段上下文取盘符与引导源；缺 EFI 盘符直接拒绝。"""
        letters: Mapping[str, str] = dict(getattr(ctx, "letters", None) or {})
        options: Mapping[str, Any] = dict(getattr(ctx, "options", None) or {})
        efi = _letter(letters.get("efi"))
        windows = _letter(letters.get("windows"))
        if not efi:
            raise BootTargetError("没有 EFI 盘符，无法写入引导")
        source = str(options.get("boot_source") or "").strip()
        if source:
            return cls(
                disk_number=int(getattr(ctx, "disk_number", 0) or 0),
                efi_letter=efi,
                windows_letter=windows,
                source=source,
                source_from_settings=True,
            )
        if not windows:
            raise BootTargetError("没有 Windows 盘符，无法写入引导")
        # ``sysBoot`` 是离线引导文件目录：镜像里没有 Windows\\Boot 时
        # （精简/自定义镜像）用它当 bcdboot 的源目录。
        return cls(
            disk_number=int(getattr(ctx, "disk_number", 0) or 0),
            efi_letter=efi,
            windows_letter=windows,
            source=f"{windows}:\\Windows",
        )


__all__ = ["BootTarget", "BootTargetError"]
