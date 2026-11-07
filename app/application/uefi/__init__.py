"""UEFI 引导子域：写引导（bcdboot）与"装完能不能开机"的校验合成一步。

**为什么单独一个包**：``bcdboot`` 与"查 ESP / 系统分区里的文件"是同一件事的
两半——写完必须马上验，验的时候用的还是同一组盘符。以前它们分在
``phases/system.py`` 的 ``BootPhase`` 与 ``phases/verify.py`` 的
``BootVerifyPhase`` 两个阶段，靠编排层"系统组 → 校验组"的行序约束，
而"校验必须发生在释放 EFI 盘符之前"这条约束只写在注释里。
合成一个阶段以后，"写 → 验"是一个对象内部的顺序。

包内分工：

- :mod:`app.application.uefi.target` —— ``BootTarget``：唯一从
  ``PhaseContext`` 提取盘符 / 引导源的地方；
- :mod:`app.application.uefi.writer` —— ``BcdbootWriter``：拼命令并执行；
- :mod:`app.application.uefi.probe` —— ``BootProbe`` + ``boot_verdict``：
  只读校验（引导文件 + 系统文件）；
- :mod:`app.application.uefi.phase` —— ``UefiBootPhase``：对外唯一的阶段门面。

``writer`` / ``probe`` 连 ``PhaseContext`` 都不认识（只吃 ``BootTarget``），
因此可以脱离编排单测；它们也不认识 Ghost、分区表或 diskpart。

注意区分：``app.modules.boot`` 是"修复已有引导"的独立功能（有自己的
``/api/boot/report`` 与界面入口），不参与写盘流水线。
"""

from app.application.uefi.phase import UefiBootPhase
from app.application.uefi.probe import BootProbe, boot_verdict
from app.application.uefi.target import BootTarget, BootTargetError
from app.application.uefi.writer import BcdbootWriter

__all__ = [
    "BcdbootWriter",
    "BootProbe",
    "BootTarget",
    "BootTargetError",
    "UefiBootPhase",
    "boot_verdict",
]
