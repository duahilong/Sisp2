"""程序基础版本号（仓库中的唯一来源）。

``/api/info`` 会把它显示在界面上，打包脚本也用它写 Windows 版本资源。
仓库中只保存 ``X.Y.Z``；CI 构建时通过 ``packaging/build.py --version``
临时注入 ``X.Y.Z-beta.N``，并在打包结束后恢复这里的源码。
"""

from __future__ import annotations

__version__ = "1.5.0"
