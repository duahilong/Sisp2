"""公共基础模块。

对外只暴露 PowerShell 执行、扫描超时常量、CLI 步骤输出和系统盘盘符；
业务模块统一从 ``app.modules.common.service`` 导入，不要在这里堆积业务逻辑。
"""

from app.modules.common.service import (
    POWERSHELL_SCAN_TIMEOUT,
    get_system_drive_letter,
    print_step_results,
    run_powershell,
)

__all__ = [
    "POWERSHELL_SCAN_TIMEOUT",
    "get_system_drive_letter",
    "print_step_results",
    "run_powershell",
]
