"""写 UEFI 引导（bcdboot）。

只认 ``BootTarget``：不读 ``PhaseContext``、不认识 Ghost / 分区表 / diskpart。
``command()`` 是纯函数（可预览、可断言），``write()`` 才真的 spawn。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.application.runner_utils import tail
from app.application.shell import BCDBOOT_TIMEOUT
from app.application.uefi.target import BootTarget


@dataclass(frozen=True)
class BootWrite:
    """一次 bcdboot 的结果（不含阶段语义，由门面翻成 ``PhaseResult``）。"""

    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class BcdbootWriter:
    """写引导：``bcdboot <源> /s <ESP>: /f UEFI``（只写文件，不写 NVRAM）。"""

    def __init__(self, shell: Any, bcdboot: str | None = None) -> None:
        self.shell = shell
        self.bcdboot = bcdboot or getattr(shell, "bcdboot", None)

    def command(self, target: BootTarget) -> list[str]:
        if not self.bcdboot:
            raise RuntimeError("找不到 bcdboot.exe，无法写入引导")
        return [self.bcdboot, target.source, "/s", f"{target.efi_letter}:", "/f", "UEFI"]

    def write(self, target: BootTarget) -> BootWrite:
        try:
            code, output, elapsed = self.shell.run(self.command(target), BCDBOOT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return BootWrite(ok=False, message=f"bcdboot 无法执行：{exc}")
        if code != 0:
            return BootWrite(
                ok=False,
                message=f"bcdboot 失败（退出码 {code}）：{tail(output, 800)}",
                details={"exit": code, "seconds": round(elapsed, 1)},
            )
        return BootWrite(
            ok=True,
            message=f"已写入 UEFI 引导（{target.efi_letter}: ← {target.source}）",
            details={"exit": code, "seconds": round(elapsed, 1), "tail": tail(output, 800)},
        )


__all__ = ["BcdbootWriter", "BootWrite"]
