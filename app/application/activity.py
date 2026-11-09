"""进程内「每块盘现在在干什么」的登记表——**只服务诊断日志**。

## 为什么需要

现场反馈：4 块盘批量装机，2 块在「硬盘分区」报
``0x80070017``（数据错误/循环冗余检查）。靠猜没法定位，而当时真正的关键信息
是「这块盘跑 diskpart 的那一刻，**别的盘是不是正在写镜像**」——分区阶段虽然
全局串行（``PartitionSequencer``），但流水线允许 A 盘写镜像的同时 B 盘分区，
两条 I/O 走同一条 USB/SATA 链路时，链路差的盘就可能读回坏数据。

所以这里维护一张极简登记表：盘号 → 当前在哪个阶段。分区阶段在写日志与失败
详情里带上「其它盘活动」，下次复现时能直接回答这个问题，不用再猜。

## 约定

- 只在**本进程**内有意义（任务状态本来也只在内存里，单进程单 worker）；
- 任何异常都不能影响执行：所有函数都不抛，最坏情况下退化成"没有信息"；
- 只登记“正在动盘”的阶段（分区 / 写镜像 / 引导 / 收尾），排队中不登记。
"""

from __future__ import annotations

import threading

__all__ = ["begin", "describe", "end", "reset", "snapshot"]

_PHASE_TEXT: dict[str, str] = {
    "partition": "分区（diskpart）",
    "image": "写镜像（Ghost）",
    "boot": "写引导（bcdboot）",
    "system": "写镜像/引导",
    "extras": "收尾拷贝",
}

_LOCK = threading.Lock()
_STATE: dict[int, str] = {}


def begin(disk_number: int, phase: str) -> None:
    """登记某块盘进入某阶段（后写的覆盖先写的）。"""
    try:
        with _LOCK:
            _STATE[int(disk_number)] = str(phase)
    except Exception:  # noqa: BLE001, S110 - 诊断信息丢了也不能影响执行
        pass


def end(disk_number: int) -> None:
    """某块盘结束了（任务收尾/异常退出都要调）。"""
    try:
        with _LOCK:
            _STATE.pop(int(disk_number), None)
    except Exception:  # noqa: BLE001, S110
        pass


def snapshot() -> dict[int, str]:
    """当前所有盘的状态副本（测试与调试用）。"""
    try:
        with _LOCK:
            return dict(_STATE)
    except Exception:  # noqa: BLE001
        return {}


def reset() -> None:
    """清空登记表（测试用；生产不需要）。"""
    try:
        with _LOCK:
            _STATE.clear()
    except Exception:  # noqa: BLE001, S110
        pass


def describe(exclude: int | None = None, *, style: str = "short") -> str:
    """把「除 ``exclude`` 之外的盘在干什么」写成一句话。

    ``style="short"`` 用于日志行（``磁盘 3=写镜像（Ghost）``）；
    ``style="plain"`` 用于把同样的信息塞进步骤详情（不带“磁盘”前缀）。
    """
    try:
        with _LOCK:
            items = sorted(_STATE.items())
    except Exception:  # noqa: BLE001
        return ""
    parts: list[str] = []
    for number, phase in items:
        if exclude is not None and int(number) == int(exclude):
            continue
        text = _PHASE_TEXT.get(phase, phase)
        parts.append(f"{number}={text}" if style == "plain" else f"磁盘 {number}={text}")
    return "；".join(parts)
