"""公共基础模块。

对外只暴露 PowerShell 执行与扫描超时常量；业务模块统一从
``app.modules.common.service`` 导入，不要在这里堆积业务逻辑。
"""

from app.modules.common.service import POWERSHELL_SCAN_TIMEOUT, run_powershell

__all__ = [
    "POWERSHELL_SCAN_TIMEOUT",
    "run_powershell",
]
