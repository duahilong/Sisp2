"""全局测试夹具。

一件事是"测试绝不该碰真实磁盘"这条底线：

``block_real_disk_operations``：**任何**真实写盘命令（diskpart / format /
   bcdboot / ghost / 写盘 cmdlet）在测试里都必须被 ``app.safety_guard`` 拦下。
   这是 2026-11-06 清盘事故的回归防线：那次测试里对 ``runner._run_diskpart``
   的直接赋值 mock 静默失效，本机真的执行了 ``clean`` + ``format``。

另一件事是**注入式替身**（分区/装机解耦之后的标准写法）：

- ``fake_shell``：``DiskpartShell`` 的替身（脚本 → 输出 + 假的真实分区表）；
- ``fake_process``：``ProcessShell`` 的替身（Ghost / bcdboot / 校验脚本）；
- ``make_runner``：用上面两个替身拼一个 ``WindowsDiskRunner``；
- ``make_snapshot``：按分区计划生成一份"完美的真实分区表" payload。

约定：测试**不要**再写 ``runner._run_diskpart = fake`` 这种"给私有方法赋值"。
实现搬家时它不会报错，只会静默失效——那正是上次清盘事故的形态。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def block_real_disk_operations(monkeypatch):
    """测试里**永远**不允许真实写盘。

    两层保护：

    1. 环境变量 ``SISP_BLOCK_REAL_DISK=1``：``app.safety_guard`` 拦下
       ``diskpart`` / ``format`` / ``bcdboot`` / ``ghost64`` 以及含写盘 cmdlet 的
       PowerShell 命令；
    2. ``WindowsDiskRunner`` / ``BootRepairer`` 默认 ``allow_real_disk=False``：
       即使环境变量被清掉，直接构造的实例也执行不了危险命令。

    需要"真的执行"的集成测试必须显式设置 ``SISP_BLOCK_REAL_DISK=0`` 并且用
    ``allow_real_disk=True`` 构造，且只能在专门的机器上手动运行，不进默认测试集。
    """
    monkeypatch.setenv("SISP_BLOCK_REAL_DISK", "1")


# ---------------------------------------------------------------- 夹具
@pytest.fixture(autouse=True)
def app_logger_isolated():
    r"""用例结束后把 ``app`` logger 的 handler / 级别还原。

    ``configure_logging()`` 会把 RotatingFileHandler 挂到 ``app`` logger 上，
    在同一进程里一直有效。不还原的话，后面用例的日志会写进前面用例的临时
    目录；更糟的是会写进真实的 ``%APPDATA%\Sisp\logs\app.log``（污染用户目录，
    而且日志内容互相串台，排查时看不见真正的那条）。
    """
    logger = logging.getLogger("app")
    before_handlers = list(logger.handlers)
    before_level = logger.level
    yield
    for handler in list(logger.handlers):
        if handler not in before_handlers:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(before_level)


@pytest.fixture(autouse=True)
def program_lookup_isolated():
    """用例之间清掉 ``find_program`` 的进程级缓存。

    ``find_program`` 现在带 ``functools.cache``（生产里 PATH 在执行期间不变，缓存是
    对的）；但用例会 monkeypatch ``shutil.which`` / ``SYSTEMROOT``（比如验证“没装
    Ghost 就该阻断”），不清缓存就会把上一个用例的探测结果带进来。
    """
    from app.application.shell import find_program

    reset = getattr(find_program, "cache_clear", None)
    if reset is not None:
        reset()
    yield
    if reset is not None:
        reset()


@pytest.fixture(autouse=True)
def ghost_tool_present(monkeypatch):
    """单测里把 Ghost 当成"已安装"。

    计划期的镜像校验会检查 ``ghost64.exe`` 在不在（真实产品行为：没装 Ghost 就该拦）。
    CI/VM 上不一定装了 Ghost，所以默认把 ``find_program`` 对 ghost* 的查询替换成
    固定路径；要验证"没装 Ghost → 阻断"的用例自己 monkeypatch 回来。
    """
    from app.application import real_runner as real_runner_module
    from app.application import workflow as workflow_module

    def _patch(module):
        real = module.find_program

        def fake(name):
            if str(name).lower().startswith("ghost"):
                return "C:/tools/ghost64.exe"
            return real(name)

        monkeypatch.setattr(module, "find_program", fake)

    _patch(workflow_module)
    _patch(real_runner_module)


from tests.fakes import (
    FakeDiskpartShell,
    FakeProcess,
    FakeUefiBoot,
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
def fake_uefi():
    """``FakeUefiBoot`` 工厂（默认写引导 + 校验都成功）。"""
    return FakeUefiBoot


@pytest.fixture
def make_snapshot():
    """``build_snapshot`` 工厂（按计划造完美分区表）。"""
    return build_snapshot


@pytest.fixture
def make_runner(fake_shell, fake_process, fake_uefi):
    """用替身拼一个 ``WindowsDiskRunner``（不注入就现造默认替身）。"""
    from app.application.real_runner import WindowsDiskRunner

    def factory(
        *,
        shell: Any = None,
        process: Any = None,
        uefi: Any = None,
        **kwargs: Any,
    ):
        # 外部工具路径给死值：否则测试会跟着“本机装没装 ghost64”飘。
        kwargs.setdefault("diskpart", "diskpart.exe")
        kwargs.setdefault("ghost", "ghost64.exe")
        kwargs.setdefault("bcdboot", "bcdboot.exe")
        return WindowsDiskRunner(
            shell=shell if shell is not None else fake_shell(),
            process=process if process is not None else fake_process(),
            uefi=uefi if uefi is not None else fake_uefi(),
            **kwargs,
        )

    return factory
