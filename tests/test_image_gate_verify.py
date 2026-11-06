"""① 镜像可应用性闸门 与 ② verify「装完能不能开机」判据。

这两个改动都是为了防止同一个后果：**盘已经清空，才发现镜像不能用 / 装不进去**。
所以测试重点不是“顺利路径”，而是每种“不该让它开始”的形态都要变成阻断项，
以及 verify 在引导文件/系统文件缺失时必须判失败。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from app.application.contracts import DiskInfo
from app.application.phases.base import PhaseContext
from app.application.phases.verify import BootVerifyPhase
from app.application.real_runner import _verify_verdict
from app.application.workflow import WorkflowController, _image_problems
from app.models import WorkerResult
from app.preflight import CHECK_IMAGE_TYPE, run_preflight_checks

_GIB = 1024**3

# 期望布局就是 EFI + C + D：``convert gpt`` 自动建的 16 MB MSR 会被执行器删掉，
# 所以 verify 只认 3 个分区（多一个 Reserved 就判失败，见 test_partition_layout）。
_OK_VERIFY_PAYLOAD: dict[str, Any] = {
    "partition_style": "GPT",
    "partitions": [
        {"number": 1, "size_bytes": 100 * 1024**2, "type": "System"},
        {"number": 2, "size_bytes": 120 * _GIB, "drive_letter": "G"},
        {"number": 3, "size_bytes": 300 * _GIB, "drive_letter": "J"},
    ],
    "boot_file": r"F:\EFI\Microsoft\Boot\bootmgfw.efi",
    "system_file": r"G:\Windows\System32\config\SYSTEM",
}


# --------------------------------------------------------- ① 镜像可应用性闸门
def _gho(tmp_path, name: str = "win11.GHO"):
    gho = tmp_path / name
    gho.write_bytes(bytes([0xFE, 0xEF]) + bytes(8))
    return gho


def test_image_problems_accepts_valid_gho(tmp_path):
    gho = _gho(tmp_path)
    assert _image_problems({"image_info": {"image_path": str(gho)}}) == []


def test_image_problems_rejects_iso_with_actionable_message(tmp_path):
    """ISO 是“能打开、不能还原”的典型：必须在计划阶段挡住，而不是清盘后报错。"""
    iso = tmp_path / "win11.iso"
    iso.write_bytes(b"iso")
    problems = _image_problems({"image_info": {"image_path": str(iso)}})
    assert problems
    assert "ISO" in problems[0]
    assert "GHO" in problems[0]


def test_image_problems_rejects_unknown_suffix(tmp_path):
    note = tmp_path / "notes.txt"
    note.write_text("x", encoding="utf-8")
    problems = _image_problems({"image_info": {"image_path": str(note)}})
    assert problems
    assert "不支持" in problems[0]


def test_image_problems_rejects_legacy_dism_formats(tmp_path):
    """WIM/ESD/SWM 走的是已经移除的 dism 路线，必须在计划期就说清楚。"""
    for name in ("a.wim", "b.esd", "c.swm"):
        target = tmp_path / name
        target.write_bytes(b"x")
        problems = _image_problems({"image_info": {"image_path": str(target)}})
        assert problems and "dism 路线已移除" in problems[0]


def test_image_problems_reports_missing_file(tmp_path):
    problems = _image_problems({"image_info": {"image_path": str(tmp_path / "gone.GHO")}})
    assert problems
    assert "不存在" in problems[0]


def test_image_problems_rejects_wrong_magic(tmp_path):
    """后缀对、文件头不对：不是 Ghost 镜像，计划期就该拦住。"""
    fake = tmp_path / "fake.GHO"
    fake.write_bytes(b"PK")
    problems = _image_problems({"image_info": {"image_path": str(fake)}})
    assert problems
    assert "文件头" in problems[0] or "不是 Ghost 镜像" in problems[0]


def test_image_problems_reports_missing_ghost_engine(tmp_path):
    gho = _gho(tmp_path)
    problems = _image_problems(
        {"image_info": {"image_path": str(gho)}}, ghost_available=False
    )
    assert problems
    assert "Ghost 可执行文件" in problems[0]


def _disk(number: int = 1) -> DiskInfo:
    return DiskInfo(
        disk_number=number,
        model="test disk",
        serial_number=None,
        unique_id=f"unique-{number}",
        size_bytes=60 * _GIB,
        size_display="60 GB",
        partition_style="RAW",
        bus_type="NVMe",
        drive_letters=(),
        is_boot=False,
        is_system=False,
        is_offline=False,
        is_read_only=False,
        is_selectable=True,
        protection_reasons=(),
    )


class _FakeService:
    def __init__(self, disks: list[DiskInfo]) -> None:
        self.disks = disks
        self.dry_run = True
        self.allowed_disk_numbers = frozenset()

    def scan_protected_disks(self, excluded_names: Any = None) -> list[DiskInfo]:
        return list(self.disks)

    def build_selection_plan(self, payload, summaries, selected, **kwargs):
        letters = {number: {"efi": "E", "windows": "F", "data": "G"} for number in selected}
        return SimpleNamespace(drive_letter_allocations=letters)

    def execute_spec(self, spec, cancel_event):
        return WorkerResult.dry_run_plan(int(getattr(spec, "disk_number", 0) or 0))

    def check_config(self, config_path=None):
        return SimpleNamespace(passed=True, message="配置可解析", error_code=0)


def _controller(image_path: str) -> WorkflowController:
    def preflight(config_path=None, dry_run=False):
        return {
            "results": [{"name": "运行前检查", "passed": True, "message": "ok"}],
            "config_payload": {
                "partition_info": {
                    "efi_size_mb": 300,
                    "c_size_gb": 10,
                    # 单测用小尺寸测算术：后端下限（C ≥ 60 GB）显式放宽
                    "allow_small_partitions": True,
                },
                "image_info": {"image_path": image_path, "image_index": 1},
            },
        }

    return WorkflowController(
        service=_FakeService([_disk(1)]),
        dry_run=True,
        preflight_runner=preflight,
    )


def test_prepare_blocks_iso_image_and_disables_start(tmp_path):
    iso = tmp_path / "win11.iso"
    iso.write_bytes(b"iso")
    controller = _controller(str(iso))
    controller.start(None, dry_run=True)

    prepared = controller.prepare([1])[0]

    assert prepared.plan.executable is False
    assert any("ISO" in blocker for blocker in prepared.plan.blockers)
    checks = {check.key: check for check in prepared.plan.checks}
    assert checks["image"].passed is False
    assert "ISO" in checks["image"].message


def test_prepare_keeps_applicable_image_executable(tmp_path):
    wim = _gho(tmp_path)
    controller = _controller(str(wim))
    controller.start(None, dry_run=True)

    prepared = controller.prepare([1])[0]

    checks = {check.key: check for check in prepared.plan.checks}
    assert checks["image"].passed is True
    assert not any("镜像" in blocker for blocker in prepared.plan.blockers)


def test_preflight_flags_iso_image(tmp_path):
    iso = tmp_path / "win11.iso"
    iso.write_bytes(b"iso")
    report = run_preflight_checks(
        dry_run=True,
        admin_checker=lambda: True,
        command_lookup=lambda name: "found",
        settings={"sysWim": str(iso)},
    )
    results = {item["name"]: item for item in report["results"]}
    assert results[CHECK_IMAGE_TYPE]["passed"] is False
    assert "ISO" in results[CHECK_IMAGE_TYPE]["message"]
    assert report["all_passed"] is False


def test_preflight_accepts_applicable_image(tmp_path):
    wim = _gho(tmp_path)
    report = run_preflight_checks(
        dry_run=True,
        admin_checker=lambda: True,
        command_lookup=lambda name: "found",
        settings={"sysWim": str(wim)},
    )
    results = {item["name"]: item for item in report["results"]}
    assert results[CHECK_IMAGE_TYPE]["passed"] is True


# ------------------------------------------------- ② verify：装完能不能开机
def test_verify_verdict_accepts_ready_disk():
    ok, message, details = _verify_verdict(_OK_VERIFY_PAYLOAD)
    assert ok is True
    assert "bootmgfw.efi" in message
    assert details["boot_file"].endswith("bootmgfw.efi")
    assert details["system_file"].endswith("SYSTEM")
    assert details["problems"] == []


def test_verify_verdict_fails_without_boot_file():
    payload = dict(_OK_VERIFY_PAYLOAD, boot_file=None)
    ok, message, details = _verify_verdict(payload)
    assert ok is False
    assert "bootmgfw" in message
    assert any("EFI" in problem for problem in details["problems"])


def test_verify_verdict_fails_without_system_file():
    """镜像只应用了一半（或只拷了引导文件）时必须判失败，而不是“六步全绿”。"""
    payload = dict(_OK_VERIFY_PAYLOAD, system_file=None)
    ok, message, _ = _verify_verdict(payload)
    assert ok is False
    assert "系统文件" in message


def test_verify_verdict_requires_gpt_and_three_partitions():
    mbr = dict(_OK_VERIFY_PAYLOAD, partition_style="MBR")
    ok, message, _ = _verify_verdict(mbr)
    assert ok is False
    assert "GPT" in message

    two_parts = dict(_OK_VERIFY_PAYLOAD, partitions=_OK_VERIFY_PAYLOAD["partitions"][:2])
    ok, message, _ = _verify_verdict(two_parts)
    assert ok is False
    assert "分区数" in message


class _StubLog:
    def __init__(self) -> None:
        self.blocks: list[str] = []

    def write(self, message: str) -> None:
        pass

    def block(self, title: str, text: str) -> None:
        self.blocks.append(title)

    def flush(self) -> None:
        pass


def test_verify_script_receives_letters(monkeypatch):
    """verify 要靠 EFI/Windows 盘符去查文件，所以必须把它们传进 PowerShell 环境。"""
    import threading

    from app.application.command_log import CommandLog
    from app.application.partitions import plan_partitions
    from app.application.shell import ProcessShell

    captured: dict[str, Any] = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_OK_VERIFY_PAYLOAD).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr("app.application.shell.subprocess.run", fake_run)
    payload = {
        "partition_info": {
            "efi_size_mb": 100,
            "c_size_gb": 120,
            "data_size_gb": "auto",
            "allow_small_partitions": True,
        }
    }
    letters = {"efi": "F", "windows": "G", "data": "H"}
    parts = plan_partitions(payload, letters=letters)
    shell = ProcessShell(
        log=CommandLog(None), allow_real_disk=False, source="verify-test"
    )
    phase = BootVerifyPhase(shell, powershell="powershell.exe")
    ctx = PhaseContext(
        disk_number=1,
        parts=parts,
        letters=letters,
        image_path=None,
        image_index=1,
        cancel=threading.Event(),
        log=CommandLog(None),
    )
    outcome = phase.run(ctx)

    assert outcome.ok is True
    assert outcome.results[-1].status == "ok"
    assert captured["env"]["VIX_EFI_LETTER"] == "F"
    assert captured["env"]["VIX_WINDOWS_LETTER"] == "G"


def test_every_preflight_check_has_a_stable_key():
    """新增预检项必须同步 ``_CHECK_KEYS``，否则界面拿到的是 check_N 这种漂移 key。"""
    from app import preflight
    from app.application.workflow import _CHECK_KEYS

    names = [
        value
        for name, value in vars(preflight).items()
        if name.startswith("CHECK_") and isinstance(value, str)
    ]
    missing = [name for name in names if name not in _CHECK_KEYS]
    assert missing == []


def test_preflight_iso_check_maps_to_image_type_key(tmp_path):
    from app.application.workflow import _build_check_items

    iso = tmp_path / "win11.iso"
    iso.write_bytes(b"iso")
    report = run_preflight_checks(
        dry_run=True,
        admin_checker=lambda: True,
        command_lookup=lambda name: "found",
        settings={"sysWim": str(iso)},
    )
    checks = {item.key: item for item in _build_check_items(report["results"])}
    assert checks["image_type"].passed is False
    assert "ISO" in checks["image_type"].message


# ------------------------------------- 优先级：设置 vs 配置 vs 向导逐盘覆盖
def _settings_controller(settings: dict, image_path: str) -> WorkflowController:
    def preflight(config_path=None, dry_run=False):
        return {
            "results": [{"name": "运行前检查", "passed": True, "message": "ok"}],
            "config_payload": {
                # 业务配置里的占位镜像：界面设置优先时应该被压掉
                "partition_info": {
                    "efi_size_mb": 100,
                    "c_size_gb": 30,
                    "allow_small_partitions": True,
                },
                "image_info": {"image_path": image_path, "image_index": 1},
            },
        }

    return WorkflowController(
        service=_FakeService([_disk(1)]),
        dry_run=True,
        preflight_runner=preflight,
        settings_provider=lambda: dict(settings),
    )


def test_prepare_reflects_live_settings_image(tmp_path):
    """改设置里的 sysWim 之后，**下一次 prepare** 就要用它，不用重启会话。

    以前 ``_apply_image_defaults`` 在 prepare 里以 override=False 调用，会话载荷
    里已经有镜像路径就直接返回，于是设置页改了镜像也不生效（第 4 步仍报旧阻断）。
    """
    iso = tmp_path / "win11.iso"
    iso.write_bytes(b"iso")
    good = _gho(tmp_path)

    settings: dict[str, Any] = {"sysWim": str(iso), "imageDefaultIndex": 1}
    controller = _settings_controller(settings, str(iso))
    controller.start(None, dry_run=True)

    blocked = controller.prepare([1])[0]
    assert blocked.plan.executable is False
    assert any("ISO" in blocker for blocker in blocked.plan.blockers)

    settings["sysWim"] = str(good)
    fixed = controller.prepare([1])[0]
    assert fixed.plan.executable is True
    assert not any("镜像" in blocker for blocker in fixed.plan.blockers)
    assert fixed.spec.config_payload["image_info"]["image_path"] == str(good)


def test_per_disk_overrides_beat_settings_defaults(tmp_path):
    """向导第 2/3 步的逐盘输入必须压过设置里的默认值（否则用户填了也被改回去）。"""
    wim = _gho(tmp_path)
    other = _gho(tmp_path, "other.GHO")

    settings: dict[str, Any] = {
        "sysWim": str(wim),
        "imageDefaultIndex": 1,
        "efiDefaultMb": 100,
        "sysDefaultGb": 100,
        "dataDefaultGb": 200,
        "defaultFs": "NTFS",
    }
    controller = _settings_controller(settings, str(wim))
    controller.start(None, dry_run=True)

    prepared = controller.prepare({1: {"c_size": 30, "data_size": 20, "win_gho": str(other)}})[0]
    info = prepared.spec.config_payload["partition_info"]

    assert info["c_size_gb"] == 30
    assert info["data_size_gb"] == 20
    assert prepared.spec.config_payload["image_info"]["image_path"] == str(other)


def test_image_path_is_normalized_for_ghost(tmp_path):
    """设置里写 ``D:/x/test.GHO`` 也要被规范化成展开后的绝对路径。"""
    from pathlib import Path as _Path

    wim = _gho(tmp_path)
    slashed = str(wim).replace("\\", "/")
    assert "/" in slashed or _Path(wim).drive  # 单字符路径在 Linux 上无分隔符，跳过

    controller = _settings_controller({"sysWim": slashed, "imageDefaultIndex": 1}, slashed)
    controller.start(None, dry_run=True)
    prepared = controller.prepare([1])[0]

    assert prepared.spec.config_payload["image_info"]["image_path"] == str(wim)
