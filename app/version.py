"""程序版本号（唯一来源）。

``/api/info`` 会把它显示在界面上，打包脚本也用它写 Windows 版本资源；
改版本只改这里，``tests/test_packaging.py`` 会检查 ``packaging/version_info.txt``
与它保持一致。
"""

from __future__ import annotations

__version__ = "1.0.0"

# Windows 版本资源用四段数字，这里给出对应关系
VERSION_TUPLE = tuple(int(part) for part in __version__.split(".")) + (0,) * (
    4 - len(__version__.split("."))
)
