"""Ghost 镜像写入：命令拼装、目标换算、护栏、失败路径、计划期阻断。

这里的用例守护四件事：

1. **命令形状**：``dst`` 是 Ghost 自己的 1 基编号、带 ``-sure/-batch/-ntexact``、
   绝不带 ``-rb``（带了下游 bcdboot 就没机会跑）；
2. **目标换算**：分区号来自计划（分区验收保证计划=实际），磁盘号要 +1；
3. **护栏**：``ghost64 -clone`` 在没有显式授权时必须被拦下，``-ver`` 放行；
4. **失败可诊断**：退出码非 0 时读 ``ghosterr.txt``，而不是只报一个数字。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.application.command_log import CommandLog
from app.application.partitions import (
    PartitionLayoutError,
    ghost_target,
    plan_partitions,
)
from app.application.phases.base import PhaseContext
from app.application.phases.ghost import GhostImagePhase
from app.application.shell import ProcessShell
from app.modules.ghost.service import (
    GHO_MAGIC,
    ghost_command,
    parse_ghosterr,
    sniff_gho,
)
from app.modules.image.service import (
    ImageError,
    configured_versions,
    find_version,
    inspect_image,
)
from app.safety_guard import RealDiskBlocked, ensure_command_allowed, is_dangerous_command

PAYLOAD = {
    "partition_info": {
        "efi_size_mb": 100,
        "c_size_gb": 3,
        "data_size_gb": "auto",
        # 单测用小尺寸：显式放宽后端下限（C ≥ 60 GB）
        "allow_small_partitions": True,
    },
    "image_info": {"image_path": "D:/images/test.GHO"},
}
LETTERS = {"efi": "E", "windows": "F", "data": "G"}


def _parts(payload=None):
    return plan_partitions(payload or PAYLOAD, letters=LETTERS)


def _ctx(tmp_path, *, parts=None, image=None, options=None, cancel=False):
    gho = image or (tmp_path / "test.GHO")
    if not Path(gho).exists():
        Path(gho).write_bytes(GHO_MAGIC + b"\x01\x00" * 32)
    return PhaseContext(
        disk_number=2,
        parts=parts if parts is not None else _parts(),
        letters=LETTERS,
        image_path=str(gho),
        cancel=_cancelled() if cancel else threading.Event(),
        log=CommandLog(None),
        options=options or {},
        on_step=None,
    )


def _cancelled():
    event = threading.Event()
    event.set()
    return event


def _phase(
    commands: list[list[str]],
    *,
    code: int = 0,
    output: str = "ok",
    ghost: str | None = "ghost64.exe",
    cwd: str | None = None,
    on_run=None,
):
    """用替身 runner 构造 GhostImagePhase，记录命令与 cwd。"""
    cwds: list[str | None] = []

    def fake_run(command, timeout, *, env=None, cwd=None):
        commands.append(list(command))
        cwds.append(cwd)
        if on_run is not None:
            on_run()
        return code, output, 0.1

    shell = ProcessShell(log=CommandLog(None), runner=fake_run)
    phase = GhostImagePhase(shell, ghost=ghost, cwd=cwd)
    phase.cwds = cwds  # type: ignore[attr-defined]
    return phase


# ---------------------------------------------------------------- 命令形状
def test_ghost_command_shape() -> None:
    command = ghost_command(
        r"D:\sw\ghost64.exe", r"D:\img\test.GHO", ghost_disk=3, partition=2
    )
    assert command[0] == r"D:\sw\ghost64.exe"
    assert command[1] == r"-clone,mode=pload,src=D:\img\test.GHO:1,dst=3:2"
    assert command[2:] == ["-sure", "-batch", "-ntexact"]
    # 带了 -rb 就会在 bcdboot 之前重启
    assert not any(part.startswith("-rb") for part in command)


def test_ghost_command_always_uses_first_block() -> None:
    """分区块序号不做设置：src 的后缀固定是 ``:1``（Ghost 的默认块）。"""
    command = ghost_command(
        "ghost64.exe", "D:/img/test.GHO", ghost_disk=1, partition=2
    )
    assert "src=D:/img/test.GHO:1," in command[1]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ghost_exe": ""},
        {"gho_path": ""},
        {"ghost_disk": 0},
        {"partition": 0},
    ],
)
def test_ghost_command_rejects_bad_input(kwargs: dict) -> None:
    args = {
        "ghost_exe": "ghost64.exe",
        "gho_path": "D:/img/test.GHO",
        "ghost_disk": 3,
        "partition": 2,
    }
    args.update(kwargs)
    with pytest.raises((ValueError, TypeError)):
        ghost_command(**args)


# ---------------------------------------------------------------- 目标换算
def test_ghost_target_is_one_based() -> None:
    parts = _parts()
    # Windows 的 disk 2 → Ghost 的 3；EFI 是 #1，Windows 是 #2
    assert ghost_target(2, parts) == (3, 2)
    assert ghost_target(0, parts) == (1, 2)


def test_ghost_target_keeps_windows_number_with_extra_data_partition() -> None:
    payload = {
        "partition_info": {
            "efi_size_mb": 100,
            "c_size_gb": 3,
            "data_partitions": [
                {"size_gb": 1, "file_system": "NTFS"},
                {"size_gb": "auto", "file_system": "NTFS"},
            ],
            "allow_small_partitions": True,
        },
        "image_info": {"image_path": "D:/images/test.GHO"},
    }
    parts = _parts(payload)
    assert [part.role for part in parts] == ["efi", "windows", "data", "data2"]
    assert ghost_target(1, parts) == (2, 2)


def test_ghost_target_rejects_bad_plan() -> None:
    with pytest.raises(PartitionLayoutError):
        ghost_target(1, ())
    payload = {
        "partition_info": {
            "efi_size_mb": 100,
            "c_size_gb": 3,
            "data_size_gb": "auto",
            "allow_small_partitions": True,
        },
        "image_info": {"image_path": "D:/images/test.GHO"},
    }
    parts = list(_parts(payload))
    swapped = [parts[1], parts[0], *parts[2:]]
    with pytest.raises(PartitionLayoutError):
        ghost_target(1, swapped)


# ---------------------------------------------------------------- GHO 识别
def test_sniff_gho_accepts_magic(tmp_path) -> None:
    gho = tmp_path / "ok.GHO"
    gho.write_bytes(GHO_MAGIC + b"\x00" * 16)
    info = sniff_gho(gho)
    assert info["ok"] is True
    assert info["size_bytes"] == 18


def test_sniff_gho_rejects_other_files(tmp_path) -> None:
    bad = tmp_path / "not.GHO"
    bad.write_bytes(b"PK\x03\x04")
    info = sniff_gho(bad)
    assert info["ok"] is False
    assert "文件头" in info["reason"]


def test_sniff_gho_missing_file(tmp_path) -> None:
    info = sniff_gho(tmp_path / "nope.GHO")
    assert info["ok"] is False
    assert info["exists"] is False


def test_inspect_image_gho(tmp_path) -> None:
    gho = tmp_path / "test.GHO"
    gho.write_bytes(GHO_MAGIC + b"\x00" * 8)
    data = inspect_image(gho)
    assert data["kind"] == "gho"
    assert data["magic_ok"] is True
    assert data["installable"] is True


@pytest.mark.parametrize("name", ["a.iso", "b.wim", "c.esd", "d.swm", "e.ghost"])
def test_inspect_image_rejects_non_gho(tmp_path, name: str) -> None:
    target = tmp_path / name
    target.write_bytes(b"x")
    with pytest.raises(ImageError):
        inspect_image(target)


# ---------------------------------------------------------------- 阶段
def test_phase_command_and_success(tmp_path) -> None:
    commands: list[list[str]] = []
    phase = _phase(commands)
    ctx = _ctx(tmp_path)

    outcome = phase.run(ctx)

    assert outcome.ok is True
    assert commands[0] == phase.command(ctx)
    assert commands[0][1].endswith(",dst=3:2")
    result = outcome.results[0]
    assert result.key == "image"
    assert result.details["engine"] == "ghost"
    assert result.details["image"].endswith("test.GHO")


def test_phase_failure_reads_ghosterr(tmp_path) -> None:
    commands: list[list[str]] = []
    cwd = tmp_path / "logs"
    cwd.mkdir()

    def write_ghosterr() -> None:
        (cwd / "ghosterr.txt").write_bytes("分区块错误：目标分区太小\nFATAL".encode("gbk"))

    phase = _phase(commands, code=1, output="", cwd=str(cwd), on_run=write_ghosterr)
    outcome = phase.run(_ctx(tmp_path))

    assert outcome.ok is False
    assert "退出码 1" in (outcome.message or "")
    assert "目标分区太小" in outcome.results[0].message


def test_phase_clears_stale_ghosterr(tmp_path) -> None:
    """上一次失败的 ghosterr.txt 不能当成本次原因。"""
    cwd = tmp_path / "logs"
    cwd.mkdir()
    (cwd / "ghosterr.txt").write_text("旧错误", encoding="gbk")
    phase = _phase([], code=0, cwd=str(cwd))
    outcome = phase.run(_ctx(tmp_path))
    assert outcome.ok is True
    assert not (cwd / "ghosterr.txt").exists()


def test_phase_missing_ghost_binary(tmp_path) -> None:
    phase = _phase([], ghost=None)
    outcome = phase.run(_ctx(tmp_path))
    assert outcome.ok is False
    assert "Ghost 无法执行" in outcome.results[0].message


# ------------------------------------------------- 并发冲突：启动闸门与重试
GHOSTERR_8027 = (
    "Error Number: (8027)\n"
    "Message: A GeneralException occurred\n"
    "A disk error occurred\n"
    "Win32 error: (0x00000015) The device is not ready.\n"
)


class _Gate:
    """记录被调用次数的启动闸门（``GhostStartGate`` 只需 ``wait()``）。"""

    def __init__(self) -> None:
        self.calls = 0

    def wait(self) -> float:
        self.calls += 1
        return 0.0


def _sequence_phase(
    results: list[tuple[int, str]],
    *,
    cwd=None,
    gate=None,
    on_call=None,
):
    """按调用次序返回不同退出码的 Ghost 阶段。"""
    calls: list[list[str]] = []

    def fake_run(command, timeout, *, env=None, cwd=None):
        calls.append(list(command))
        index = min(len(calls) - 1, len(results) - 1)
        code, output = results[index]
        if on_call is not None:
            on_call(len(calls))
        return code, output, 0.1

    shell = ProcessShell(log=CommandLog(None), runner=fake_run)
    phase = GhostImagePhase(shell, ghost="ghost64.exe", cwd=cwd, start_gate=gate)
    phase.calls = calls  # type: ignore[attr-defined]
    return phase


def test_phase_retries_once_after_transient_8027(tmp_path, monkeypatch) -> None:
    """并发冲突（8027 / device is not ready）自动重试一次即成功。"""
    monkeypatch.setattr("app.application.phases.ghost.RETRY_DELAY", 0.01)
    gate = _Gate()

    def on_call(index: int) -> None:
        if index == 1:
            (tmp_path / "ghosterr.txt").write_bytes(GHOSTERR_8027.encode("gbk"))

    phase = _sequence_phase(
        [(1, ""), (0, "ok")], cwd=str(tmp_path), gate=gate, on_call=on_call
    )
    outcome = phase.run(_ctx(tmp_path))

    assert outcome.ok is True
    assert len(phase.calls) == 2, "8027 属于瞬时冲突，应该重试一次"
    assert gate.calls == 2, "每次启动 Ghost 前都要过启动闸门"
    assert outcome.results[0].details["attempts"] == 2


def test_phase_gives_up_after_two_transient_failures(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.application.phases.ghost.RETRY_DELAY", 0.01)

    def on_call(index: int) -> None:
        (tmp_path / "ghosterr.txt").write_bytes(GHOSTERR_8027.encode("gbk"))

    phase = _sequence_phase([(1, ""), (1, "")], cwd=str(tmp_path), on_call=on_call)
    outcome = phase.run(_ctx(tmp_path))

    assert outcome.ok is False
    assert len(phase.calls) == 2
    assert outcome.results[0].details["attempts"] == 2
    assert "退出码 1" in outcome.results[0].message


def test_phase_does_not_retry_real_errors(tmp_path, monkeypatch) -> None:
    """不是并发冲突（GHOSTERR 里没 8027）就不重试，避免把真错误跑两遍。"""
    monkeypatch.setattr("app.application.phases.ghost.RETRY_DELAY", 0.01)

    def on_call(index: int) -> None:
        (tmp_path / "ghosterr.txt").write_bytes(
            "分区块错误：目标分区太小\nFATAL".encode("gbk")
        )

    phase = _sequence_phase([(1, ""), (0, "ok")], cwd=str(tmp_path), on_call=on_call)
    outcome = phase.run(_ctx(tmp_path))

    assert outcome.ok is False
    assert len(phase.calls) == 1
    assert outcome.results[0].details["attempts"] == 1


def test_phase_cancel_during_retry_wait(tmp_path, monkeypatch) -> None:
    """重试等待期间被取消：返回 cancelled，不再启动第二次。"""
    monkeypatch.setattr("app.application.phases.ghost.RETRY_DELAY", 0.3)
    ctx = _ctx(tmp_path)

    def on_call(index: int) -> None:
        if index == 1:
            (tmp_path / "ghosterr.txt").write_bytes(GHOSTERR_8027.encode("gbk"))
            ctx.cancel.set()

    phase = _sequence_phase([(1, ""), (0, "ok")], cwd=str(tmp_path), on_call=on_call)
    outcome = phase.run(ctx)

    assert outcome.ok is False
    assert outcome.cancelled is True
    assert len(phase.calls) == 1


def test_phase_cancel_skips(tmp_path) -> None:
    commands: list[list[str]] = []
    phase = _phase(commands)
    outcome = phase.run(_ctx(tmp_path, cancel=True))
    assert outcome.ok is False
    assert outcome.cancelled is True
    assert commands == []


def test_phase_passes_cwd_to_shell(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    phase = _phase([], cwd=str(logs))
    phase.run(_ctx(tmp_path))
    assert phase.cwds == [str(logs)]  # type: ignore[attr-defined]


def test_parse_ghosterr_keeps_tail() -> None:
    text = "a" * 1000
    out = parse_ghosterr(text, limit=10)
    assert out == "a" * 10


# ---------------------------------------------------------------- 护栏
def test_guard_blocks_ghost_clone_without_authorization() -> None:
    command = ["ghost64.exe", "-clone,mode=pload,src=D:/a.GHO:1,dst=3:2", "-sure"]
    assert is_dangerous_command(command) is True
    with pytest.raises(RealDiskBlocked):
        ensure_command_allowed(command, allow_real_disk=False, source="test")


def test_guard_allows_ghost_version_probe() -> None:
    command = ["ghost64.exe", "-ver"]
    assert is_dangerous_command(command) is False
    # 只读命令即使没授权也放行（不需要 allow_real_disk）
    ensure_command_allowed(command, allow_real_disk=False, source="test")


def test_guard_blocks_bare_ghost_interactive() -> None:
    """不带参数会打开交互界面，按危险处理。"""
    assert is_dangerous_command(["ghost64.exe"]) is True


def test_guard_blocks_ghost_inside_powershell() -> None:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "Start-Process ghost64 -ArgumentList '-clone,mode=pload'",
    ]
    assert is_dangerous_command(command) is True


# ---------------------------------------------------------------- 计划期阻断
def _problems(payload, **kwargs):
    from app.application.workflow import _image_problems

    return _image_problems(payload, **kwargs)


def _payload(path: str) -> dict:
    return {"image_info": {"image_path": path}}


def test_plan_accepts_valid_gho(tmp_path) -> None:
    gho = tmp_path / "test.GHO"
    gho.write_bytes(GHO_MAGIC + b"\x00" * 8)
    assert _problems(_payload(str(gho))) == []


def test_plan_blocks_missing_gho(tmp_path) -> None:
    problems = _problems(_payload(str(tmp_path / "nope.GHO")))
    assert problems and "镜像文件不存在" in problems[0]


def test_plan_blocks_bad_magic(tmp_path) -> None:
    bad = tmp_path / "bad.GHO"
    bad.write_bytes(b"PK\x03\x04")
    problems = _problems(_payload(str(bad)))
    assert problems and ("文件头" in problems[0] or "不是 Ghost 镜像" in problems[0])


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        ("a.iso", "ISO"),
        ("b.wim", "dism 路线已移除"),
        ("c.esd", "dism 路线已移除"),
        ("d.swm", "dism 路线已移除"),
        ("e.xyz", "只接受 GHO/GHS"),
    ],
)
def test_plan_blocks_unsupported_types(tmp_path, name: str, needle: str) -> None:
    target = tmp_path / name
    target.write_bytes(b"x")
    problems = _problems(_payload(str(target)))
    assert problems and needle in problems[0]


def test_plan_blocks_missing_ghost_engine(tmp_path) -> None:
    gho = tmp_path / "test.GHO"
    gho.write_bytes(GHO_MAGIC + b"\x00" * 8)
    problems = _problems(_payload(str(gho)), ghost_available=False)
    assert problems and "Ghost 可执行文件" in problems[0]


def test_plan_skips_engine_check_when_unknown(tmp_path) -> None:
    gho = tmp_path / "test.GHO"
    gho.write_bytes(GHO_MAGIC + b"\x00" * 8)
    assert _problems(_payload(str(gho)), ghost_available=None) == []


# ------------------------------------------------- 设置里的两个系统槽位
def test_configured_versions_skips_empty_slots():
    versions = configured_versions({"sysImageA": "", "sysImageB": ""})
    assert versions == []


def test_configured_versions_labels_and_paths():
    versions = configured_versions(
        {
            "sysImageA": "D:/images/win10.GHO",
            "sysImageALabel": "Windows 10 22H2",
            "sysImageB": "D:/images/win11.GHO",
            "sysImageBLabel": "  ",
        }
    )
    assert [item["id"] for item in versions] == ["A", "B"]
    assert versions[0]["label"] == "Windows 10 22H2"
    # 名字留空时退回“系统 B”，向导按钮上总有字
    assert versions[1]["label"] == "系统 B"


def test_find_version_matches_by_path():
    settings = {"sysImageA": "D:/images/win10.GHO", "sysImageB": "D:/images/win11.GHO"}
    assert find_version(settings, "D:/images/win11.GHO")["id"] == "B"
    assert find_version(settings, "D:/images/other.GHO") is None
