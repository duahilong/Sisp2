"""全局测试夹具。

一件事是"测试绝不该碰真实磁盘"这条底线：

``block_real_disk_operations``：**任何**真实写盘命令（diskpart / format /
   bcdboot / dism / 写盘 cmdlet）在测试里都必须被 ``app.safety_guard`` 拦下。
   这是 2026-11-06 清盘事故的回归防线：那次测试里对 ``runner._run_diskpart``
   的直接赋值 mock 静默失效，本机真的执行了 ``clean`` + ``format``。

另一件事是**注入式替身**（分区/装机解耦之后的标准写法）：

- ``fake_shell``：``DiskpartShell`` 的替身（脚本 → 输出 + 假的真实分区表）；
- ``fake_process``：``ProcessShell`` 的替身（dism / bcdboot / 校验脚本）；
- ``make_runner``：用上面两个替身拼一个 ``WindowsDiskRunner``；
- ``make_snapshot``：按分区计划生成一份"完美的真实分区表" payload。

约定：测试**不要**再写 ``runner._run_diskpart = fake`` 这种"给私有方法赋值"。
实现搬家时它不会报错，只会静默失效——那正是上次清盘事故的形态。
"""

from __future__ import annotations

from typing import Any

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


# ---------------------------------------------------------------- 夹具
from tests.fakes import (
    FakeDiskpartShell,
    FakeProcess,
    FakeVerify,
    build_snapshot,
)


@pytest.fixture
def fake_shell():
    """``FakeDiskpartShell`` 工厂。"""
    return FakeDiskpartShell


@pytest.fixture
def fake_process():
    """``FakeProcess`` 工厂。"""
    return FakeProcess


@pytest.fixture
def fake_verify():
    """``FakeVerify`` 工厂（默认校验通过）。"""
    return FakeVerify


@pytest.fixture
def make_snapshot():
    """``build_snapshot`` 工厂（按计划造完美分区表）。"""
    return build_snapshot


@pytest.fixture
def make_runner(fake_shell, fake_process, fake_verify):
    """用替身拼一个 ``WindowsDiskRunner``（不注入就现造默认替身）。"""
    from app.application.real_runner import WindowsDiskRunner

    def factory(
        *,
        shell: Any = None,
        process: Any = None,
        verify: Any = None,
        **kwargs: Any,
    ):
        return WindowsDiskRunner(
            shell=shell if shell is not None else fake_shell(),
            process=process if process is not None else fake_process(),
            verify=verify if verify is not None else fake_verify(),
            **kwargs,
        )

    return factory
