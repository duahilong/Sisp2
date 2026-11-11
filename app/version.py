"""程序版本号（唯一来源）。

``/api/info`` 会把它显示在界面上，打包脚本也用它写 Windows 版本资源；
改版本只改这里，``tests/test_packaging.py`` 会检查 ``packaging/version_info.txt``
与它保持一致。
"""

from __future__ import annotations

__version__ = "1.5.0"
