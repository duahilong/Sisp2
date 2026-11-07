"""UEFI 引导子域：BootTarget / BcdbootWriter / BootProbe / UefiBootPhase。

守的是三件事：

1. **写与验必须同序**：``bcdboot`` 之后立刻查文件，且两者用同一组盘符；
2. **判据只看引导文件 + 系统文件**：GPT / 分区数 / MSR 归分区验收，不重复查；
3. **失败只产出一个 ``boot`` 结果**：不会出现"引导写成功、机器起不来、进度全绿"。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

from app.application.command_log import CommandLog
from app.application.phases.base import PhaseContext
from app.application.shell import ProcessShell
from app.application.uefi import (
    BcdbootWriter,
    BootProbe,
    BootTarget,
    BootTargetError,
    UefiBootPhase,
)
from app.application.uefi.probe import boot_verdict
from tests.fakes import FakeProcess

_GOOD_OUTPUT = json.dumps(
    {
        "boot_file": r"E:\EFI\Microsoft\Boot\bootmgfw.efi",
        "system_file": r"F:\Windows\System32\config\SYSTEM",
    }
)


def _target(**kwargs: Any) -> BootTarget:
    base = {
        "disk_number": 1,
        "efi_letter": "E",
        "windows_letter": "F",
        "source": r"F:\Windows",
    }
    base.update(kwargs)
    return BootTarget(**base)


def _ctx(*, letters=None, options=None, on_step=None) -> PhaseContext:
    return PhaseContext(
        disk_number=1,
        parts=(),
        letters={"efi": "E", "windows": "F", "data": "G"} if letters is None else letters,
        image_path=None,
        image_index=1,
        cancel=threading.Event(),
        log=CommandLog(None),
        options=options or {},
        on_step=on_step,
    )


def _handler(*, bcdboot_code: int = 0, payload: str = _GOOD_OUTPUT):
    """bcdboot 成功 + 校验脚本返回给定 JSON。"""

    def handle(command, timeout, env):
        if command[0].endswith("bcdboot.exe"):
            return bcdboot_code, "Boot files successfully created.", 0.4
        return 0, payload, 0.2

    return handle


# --------------------------------------------------------------- BootTarget
def test_boot_target_defaults_to_windows_dir():
    target = BootTarget.from_context(_ctx())
    assert target.efi_letter == "E"
    assert target.windows_letter == "F"
    assert target.source == "F:\\Windows"
    assert target.source_from_settings is False


def test_boot_target_prefers_settings_source():
    target = BootTarget.from_context(_ctx(options={"boot_source": "D:/boot"}))
    assert target.source == "D:/boot"
    assert target.source_from_settings is True


def test_boot_target_requires_efi_letter():
    try:
        BootTarget.from_context(_ctx(letters={"windows": "F"}))
    except BootTargetError as exc:
        assert "EFI 盘符" in str(exc)
    else:  # pragma: no cover - 没抛异常就是回归
        raise AssertionError("缺 EFI 盘符时必须拒绝")


# --------------------------------------------------------------- 判据（纯函数）
def test_boot_verdict_accepts_ready_disk():
    ok, message, details = boot_verdict(
        {"boot_file": r"E:\EFI\Microsoft\Boot\bootmgfw.efi",
         "system_file": r"F:\Windows\System32\config\SYSTEM"}
    )
    assert ok is True
    assert "bootmgfw.efi" in message
    assert details["boot_file"].endswith("bootmgfw.efi")
    assert details["system_file"].endswith("SYSTEM")
    assert details["problems"] == []


def test_boot_verdict_fails_without_boot_file():
    ok, message, details = boot_verdict(
        {"boot_file": None, "system_file": r"F:\Windows\System32\config\SYSTEM"}
    )
    assert ok is False
    assert "bootmgfw" in message
    assert any("EFI" in problem for problem in details["problems"])


def test_boot_verdict_fails_without_system_file():
    """镜像只应用了一半（或只拷了引导文件）时必须判失败，而不是“六步全绿”。"""
    ok, message, _ = boot_verdict(
        {"boot_file": r"E:\EFI\Microsoft\Boot\bootmgfw.efi", "system_file": None}
    )
    assert ok is False
    assert "系统文件" in message


def test_boot_verdict_ignores_partition_layout():
    """分区表信息不再参与判定（GPT / 分区数 / MSR 由分区验收负责）。"""
    ok, _, details = boot_verdict(
        {
            "partition_style": "MBR",
            "partitions": [],
            "boot_file": r"E:\EFI\BOOT\BOOTX64.EFI",
            "system_file": r"F:\Windows\System32\ntoskrnl.exe",
        }
    )
    assert ok is True
    assert "partition_style" not in details


# --------------------------------------------------------------- 写引导
def test_writer_spells_bcdboot_arguments():
    writer = BcdbootWriter(FakeProcess(), "bcdboot.exe")
    assert writer.command(_target()) == [
        "bcdboot.exe",
        "F:\\Windows",
        "/s",
        "E:",
        "/f",
        "UEFI",
    ]


def test_writer_reports_exit_code():
    process = FakeProcess(handler=_handler(bcdboot_code=3))
    result = BcdbootWriter(process, "bcdboot.exe").write(_target())
    assert result.ok is False
    assert "退出码 3" in result.message
    assert result.details["exit"] == 3


# --------------------------------------------------------------- 探针
def test_probe_rejects_non_json_output():
    process = FakeProcess(handler=lambda command, timeout, env: (0, "not json", 0.1))
    result = BootProbe(process).inspect(_target())
    assert result.ok is False
    assert "JSON" in result.message


def test_probe_receives_letters(monkeypatch):
    """探针要靠 EFI/Windows 盘符查文件，必须把它们传进 PowerShell 环境。"""
    captured: dict[str, Any] = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        captured["command"] = list(command)
        return SimpleNamespace(returncode=0, stdout=_GOOD_OUTPUT.encode("utf-8"), stderr=b"")

    monkeypatch.setattr("app.application.shell.subprocess.run", fake_run)
    shell = ProcessShell(log=CommandLog(None), allow_real_disk=False, source="uefi-test")
    result = BootProbe(shell, powershell="powershell.exe").inspect(_target())

    assert result.ok is True
    assert captured["command"][0] == "powershell.exe"
    assert captured["env"]["VIX_EFI_LETTER"] == "E"
    assert captured["env"]["VIX_WINDOWS_LETTER"] == "F"


# --------------------------------------------------------------- 阶段门面
def test_phase_writes_then_probes():
    process = FakeProcess(handler=_handler())
    outcome = UefiBootPhase(process, bcdboot="bcdboot.exe").run(_ctx())

    assert outcome.ok is True
    assert [result.key for result in outcome.results] == ["boot"]
    assert process.calls[0][0].endswith("bcdboot.exe")
    assert process.calls[1][0].startswith("powershell")
    assert outcome.results[0].details["boot_file"].endswith("bootmgfw.efi")


def test_phase_fails_when_probe_cannot_find_system_file():
    payload = json.dumps(
        {"boot_file": r"E:\EFI\Microsoft\Boot\bootmgfw.efi", "system_file": None}
    )
    process = FakeProcess(handler=_handler(payload=payload))
    outcome = UefiBootPhase(process, bcdboot="bcdboot.exe").run(_ctx())

    assert outcome.ok is False
    assert "已写入" in outcome.results[0].message
    assert "系统文件" in outcome.message
    assert outcome.results[0].status == "failed"


def test_phase_fails_fast_when_bcdboot_fails():
    process = FakeProcess(handler=_handler(bcdboot_code=1))
    outcome = UefiBootPhase(process, bcdboot="bcdboot.exe").run(_ctx())

    assert outcome.ok is False
    assert len(process.calls) == 1  # 没写成功就不该去查文件
    assert "退出码 1" in outcome.message


def test_phase_requires_efi_letter():
    process = FakeProcess()
    outcome = UefiBootPhase(process, bcdboot="bcdboot.exe").run(
        _ctx(letters={"windows": "F"})
    )

    assert outcome.ok is False
    assert "EFI 盘符" in outcome.message
    assert process.calls == []


def test_phase_cancelled_before_boot():
    process = FakeProcess()
    ctx = _ctx()
    ctx.cancel.set()
    outcome = UefiBootPhase(process, bcdboot="bcdboot.exe").run(ctx)

    assert outcome.ok is False
    assert outcome.cancelled is True
    assert outcome.results[0].key == "boot"
    assert outcome.results[0].status == "skipped"
    assert process.calls == []
