"""全局测试夹具。

两件事，都是"测试绝不该碰真实磁盘"这条底线的组成部分：

``block_real_disk_operations``：**任何**真实写盘命令（diskpart / format /
   bcdboot / dism / 写盘 cmdlet）在测试里都必须被 ``app.safety_guard`` 拦下。
   这是 2026-11-06 清盘事故的回归防线：那次测试里对 ``runner._run_diskpart``
   的直接赋值 mock 静默失效，本机真的执行了 ``clean`` + ``format``。
"""

import pytest


@pytest.fixture(autouse=True)
def block_real_disk_operations(monkeypatch):
    """测试里**永远**不允许真实写盘。

    两层保护：

    1. 环境变量 ``SISP_BLOCK_REAL_DISK=1``：``app.safety_guard`` 拦下
       ``diskpart`` / ``format`` / ``bcdboot`` / ``dism`` 以及含写盘 cmdlet 的
       PowerShell 命令；
    2. ``WindowsDiskRunner`` / ``BootRepairer`` 默认 ``allow_real_disk=False``：
       即使环境变量被清掉，直接构造的实例也执行不了危险命令。

    需要"真的执行"的集成测试必须显式设置 ``SISP_BLOCK_REAL_DISK=0`` 并且用
    ``allow_real_disk=True`` 构造，且只能在专门的机器上手动运行，不进默认测试集。
    """
    monkeypatch.setenv("SISP_BLOCK_REAL_DISK", "1")
