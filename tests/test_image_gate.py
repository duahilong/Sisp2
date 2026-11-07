"""镜像可应用性闸门：不该让它开始的形态在计划期就阻断。

改动动机：**盘已经清空，才发现镜像不能用 / 装不进去**。所以测试重点不是
“顺利路径”，而是每种“不该让它开始”的形态都要变成阻断项。

引导校验（写引导 + 查引导文件/系统文件）与它同属“装完能不能开机”，
但代码搬到了 :mod:`app.application.uefi`，测试在 ``tests/test_uefi_boot.py``。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.application.contracts import DiskInfo
from app.application.workflow import WorkflowController, _image_problems
from app.models import WorkerResult
from app.preflight import CHECK_IMAGE_TYPE, run_preflight_checks

_GIB = 1024**3


# --------------------------------------------------------- 镜像可应用性闸门
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



def _controller(image_path: str) -> WorkflowController:
    """载荷来源只有两条：构造时的分区默认值快照 + 现读的设置（镜像路径）。"""

    def preflight(*, dry_run: bool = False) -> dict[str, Any]:
        return {"results": [{"name": "运行前检查", "passed": True, "message": "ok"}]}

    return WorkflowController(
        service=_FakeService([_disk(1)]),
        dry_run=True,
        preflight_runner=preflight,
        partition_defaults={
            "efi_size_mb": 300,
            "c_size_gb": 10,
            # 单测用小尺寸测算术：后端下限（C ≥ 60 GB）显式放宽
            "allow_small_partitions": True,
        },
        settings_provider=lambda: {"sysImageA": image_path},
    )


def test_prepare_blocks_iso_image_and_disables_start(tmp_path):
    iso = tmp_path / "win11.iso"
    iso.write_bytes(b"iso")
    controller = _controller(str(iso))
    controller.start(dry_run=True)

    prepared = controller.prepare([1])[0]

    assert prepared.plan.executable is False
    assert any("ISO" in blocker for blocker in prepared.plan.blockers)
    checks = {check.key: check for check in prepared.plan.checks}
    assert checks["image"].passed is False
    assert "ISO" in checks["image"].message


def test_prepare_keeps_applicable_image_executable(tmp_path):
    wim = _gho(tmp_path)
    controller = _controller(str(wim))
    controller.start(dry_run=True)

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
        settings={"sysImageA": str(iso)},
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
        settings={"sysImageA": str(wim)},
    )
    results = {item["name"]: item for item in report["results"]}
    assert results[CHECK_IMAGE_TYPE]["passed"] is True


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
        settings={"sysImageA": str(iso)},
    )
    checks = {item.key: item for item in _build_check_items(report["results"])}
    assert checks["image_type"].passed is False
    assert "ISO" in checks["image_type"].message


# ------------------------------------- 优先级：设置 vs 配置 vs 向导逐盘覆盖
def _settings_controller(settings: dict, image_path: str = "") -> WorkflowController:
    """``settings`` 是活字典：测试会中途改它，验证“改完不必重启”。"""

    def preflight(*, dry_run: bool = False) -> dict[str, Any]:
        return {"results": [{"name": "运行前检查", "passed": True, "message": "ok"}]}

    return WorkflowController(
        service=_FakeService([_disk(1)]),
        dry_run=True,
        preflight_runner=preflight,
        partition_defaults={
            "efi_size_mb": 100,
            "c_size_gb": 30,
            "allow_small_partitions": True,
        },
        settings_provider=lambda: dict(settings),
    )


def test_prepare_reflects_live_settings_image(tmp_path):
    """改设置里的 sysImageA 之后，**下一次 prepare** 就要用它，不用重启会话。

    以前 ``_apply_image_defaults`` 在 prepare 里以 override=False 调用，会话载荷
    里已经有镜像路径就直接返回，于是设置页改了镜像也不生效（第 4 步仍报旧阻断）。
    """
    iso = tmp_path / "win11.iso"
    iso.write_bytes(b"iso")
    good = _gho(tmp_path)
    settings: dict[str, Any] = {"sysImageA": str(iso)}

    controller = _settings_controller(settings, str(iso))
    controller.start(dry_run=True)

    blocked = controller.prepare([1])[0]
    assert blocked.plan.executable is False
    assert any("ISO" in blocker for blocker in blocked.plan.blockers)

    settings["sysImageA"] = str(good)
    fixed = controller.prepare([1])[0]
    assert fixed.plan.executable is True
    assert not any("镜像" in blocker for blocker in fixed.plan.blockers)
    assert fixed.spec.task_payload["image_info"]["image_path"] == str(good)


def test_per_disk_overrides_beat_settings_defaults(tmp_path):
    """向导第 2/3 步的逐盘输入必须压过设置里的默认值（否则用户填了也被改回去）。"""
    wim = _gho(tmp_path)
    other = _gho(tmp_path, "other.GHO")

    settings: dict[str, Any] = {
        "sysImageA": str(wim),
        "efiDefaultMb": 100,
        "sysDefaultGb": 100,
        "dataDefaultGb": 200,
    }
    controller = _settings_controller(settings, str(wim))
    controller.start(dry_run=True)

    prepared = controller.prepare({1: {"c_size": 30, "data_size": 20, "win_gho": str(other)}})[0]
    info = prepared.spec.task_payload["partition_info"]

    assert info["c_size_gb"] == 30
    assert info["data_size_gb"] == 20
    assert prepared.spec.task_payload["image_info"]["image_path"] == str(other)


def test_image_path_is_normalized_for_ghost(tmp_path):
    """设置里写 ``D:/x/test.GHO`` 也要被规范化成展开后的绝对路径。"""
    from pathlib import Path as _Path

    wim = _gho(tmp_path)
    slashed = str(wim).replace("\\", "/")
    assert "/" in slashed or _Path(wim).drive  # 单字符路径在 Linux 上无分隔符，跳过

    controller = _settings_controller({"sysImageA": slashed}, slashed)
    controller.start(dry_run=True)
    prepared = controller.prepare([1])[0]

    assert prepared.spec.task_payload["image_info"]["image_path"] == str(wim)
