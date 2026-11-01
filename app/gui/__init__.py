"""GUI 相关的公共代码包。

这里只放与具体界面框架无关的小工具（目前是提权判断），
**不 import pywebview**，所以命令行入口和单元测试也可以安全地
``import app.gui.elevation``。

真正的界面入口是 ``app.gui_web``（pywebview + 本地 HTML）。
"""

from __future__ import annotations

from app.gui.elevation import is_admin, relaunch_as_admin

__all__ = ["is_admin", "relaunch_as_admin"]
